#!/usr/bin/env python3

import datetime
import logging
import os
import re
import signal
import sys
import time

import vrnetlab

STARTUP_CONFIG_FILE = "/config/startup-config.cfg"


def handle_SIGCHLD(signal, frame):
    os.waitpid(-1, os.WNOHANG)


def handle_SIGTERM(signal, frame):
    sys.exit(0)


signal.signal(signal.SIGINT, handle_SIGTERM)
signal.signal(signal.SIGTERM, handle_SIGTERM)
signal.signal(signal.SIGCHLD, handle_SIGCHLD)

TRACE_LEVEL_NUM = 9
logging.addLevelName(TRACE_LEVEL_NUM, "TRACE")


def trace(self, message, *args, **kws):
    # Yes, logger takes its '*args' as 'args'.
    if self.isEnabledFor(TRACE_LEVEL_NUM):
        self._log(TRACE_LEVEL_NUM, message, args, **kws)


logging.Logger.trace = trace


class AR1000V_vm(vrnetlab.VM):
    def __init__(self, username, password, hostname, conn_mode):
        disk_image = None
        self.vm_type = "AR1000V"
        self.vm_version = "UNKNOWN"

        for e in sorted(os.listdir("/")):
            if not disk_image and re.search(".qcow2$", e):
                disk_image = "/" + e
                # try to detect VRP version from filename (qcow images should contain strings like V300R019)
                m = re.search(r"(V\d+R\d+)", e)
                if m:
                    self.vm_version = m.group(1)

        # default RAM und CPU values which will be used when there is no match from qcow image name
        ram = 4096
        smp = "1"

        super(AR1000V_vm, self).__init__(
            username,
            password,
            disk_image=disk_image,
            ram=ram,
            smp=smp,
            driveif="virtio",
        )

        self.hostname = hostname
        self.conn_mode = conn_mode
        self.num_nics = 6
        self.nic_type = "virtio-net-pci"
        self._pressed_any_key = False
        self._login_retry_delay = 0
        self.login_username = "super"
        self.login_password = "super"

    def bootstrap_spin(self):
        """This function should be called periodically to do work."""

        if self.spins > 300:
            # too many spins with no result -> give up and restart
            self.stop()
            self.start()
            return

        # First check for prompt
        (ridx, match, res) = self.tn.expect(
            [b"<[Hh][Uu][Aa][Ww][Ee][Ii]>", b"Press any key", b"Username:", b"Password:", b"Authentication failed"], 1
        )

        # Read any additional available output immediately
        try:
            extra_output = self.tn.read_very_eager()
        except EOFError:
            extra_output = b""

        # Combine the two (so we don’t miss anything)
        full_output = b"".join([res or b"", extra_output or b""])

        # Print/log each line received
        if full_output:
            try:
                decoded_output = full_output.decode(errors="ignore")
            except UnicodeDecodeError:
                decoded_output = str(full_output)

            retry_match = re.search(r"retry after (\d+) seconds", decoded_output, re.IGNORECASE)
            if retry_match:
                try:
                    self._login_retry_delay = int(retry_match.group(1))
                except ValueError:
                    self._login_retry_delay = 0

            for line in decoded_output.splitlines():
                if line.strip():  # skip empty lines
                    self.logger.info(f"DEVICE: {line}")
            self.spins = 0  # reset spin counter if we saw anything

        if match and ridx == 1:
            if not self._pressed_any_key:
                self.logger.info("Detected 'Press any key' prompt, sending newline")
                self.tn.write(b"\n")
                self._pressed_any_key = True
                self.spins = 0
            time.sleep(2)
            return

        if match and ridx == 2:
            self.logger.info("Detected login prompt, sending username")
            self.tn.write(f"{self.login_username}\n".encode())
            self.spins = 0
            time.sleep(2)
            return

        if match and ridx == 3:
            self.logger.info("Detected password prompt, sending password")
            self.tn.write(f"{self.login_password}\n".encode())
            self.spins = 0
            time.sleep(2)
            return

        if match and ridx == 4:
            self.logger.warning("Authentication failed, waiting before retry")
            time.sleep(self._login_retry_delay or 5)
            return

        # If prompt matched do config
        if match and ridx == 0:
            
            # fetch VRP version first
            self.logger.info("Fetching VRP version...")
            self.tn.write(b"display version\n")
            time.sleep(3)
            output = self.tn.read_until(b"<", timeout=3).decode(errors="ignore")

            # extract VRP version
            # Look for something like V800R011C00 (ignore SPC part)
            m = re.search(r"(V\d+R\d+C\d+)", output)
            if m:
                self.vm_version = m.group(1)   # only the first part
                self.logger.info(f"Detected VRP version: {self.vm_version}")
            else:
                self.vm_version = "UNKNOWN"
                self.logger.warning("Could not detect VRP version!")

            # call the startup and bootstrap methods
            self.logger.info("Running bootstrap_config()")
            self.startup_config()
            self.bootstrap_config()
            time.sleep(1)
            self.tn.close()
            startup_time = datetime.datetime.now() - self.start_time
            self.logger.info(f"Startup complete in: {startup_time}")
            self.running = True
            return

        time.sleep(5)
        self.spins += 1

        return

    def bootstrap_mgmt_interface(self):
        # wait for system to become ready for configuration
        # otherwise we might see Error: The system is busy in building configuration. Please wait for a moment...
        self.logger.info("bootstrap_mgmt_interface - Sleeping for another 60s to wait for the system to become ready...()")
        time.sleep(60)
        self.wait_write(cmd="system-view", wait=">")
        self.wait_write(cmd="ip vpn-instance __MGMT_VPN__", wait="]")
        self.wait_write(cmd="ipv4-family", wait="]")
        self.wait_write(cmd="quit", wait="]")
        self.wait_write(cmd="quit", wait="]")
        mgmt_interface = "GigabitEthernet"
        interface_name = f"{mgmt_interface}0/0/0"
        self.wait_write(cmd=f"interface {interface_name}", wait="]")
        self.wait_write(cmd="undo shutdown", wait=None)
        self.wait_write(cmd="ip binding vpn-instance __MGMT_VPN__", wait="]")
        self.wait_write(cmd=f"ip address {self.mgmt_address_ipv4.replace('/', ' ')}", wait="]")
        self.wait_write(cmd="quit", wait="]")
        self.wait_write(
            cmd=f"ip route-static vpn-instance __MGMT_VPN__ 0.0.0.0 0 {self.mgmt_gw_ipv4}", wait="]"
        )

    def bootstrap_config(self):
        """Do the actual bootstrap config"""

    # Example: conditional config
        if getattr(self, "vm_version", None) == "V800R023C00":
            self.logger.info("Applying config for VRP version V800R023C00")
            # run R23 specific commands here
            #self.wait_write(cmd="undo user-security-policy enable", wait="]")
            #self.wait_write(cmd="undo dcn", wait="]")
        if getattr(self, "vm_version", None) == "V800R011C00":
            self.logger.info("Applying config for VRP version V800R011C00")
            # run R11 specific commands here
            #self.wait_write(cmd="undo user-security-policy enable", wait="]")
            #self.wait_write(cmd="undo dcn", wait="]")

    # Default / generic config here
        self.logger.info("Applying generic bootstrap config...")
        # ... rest of your existing bootstrap_config logic ...


        self.bootstrap_mgmt_interface()
        self.wait_write(cmd=f"sysname {self.hostname}", wait="]")

        self.wait_write(cmd="aaa", wait="]")
        self.wait_write(cmd=f"undo local-user {self.username}", wait="]")
        self.wait_write(
            cmd=f"local-user {self.username} password irreversible-cipher {self.password}",
            wait="]",
        )
        self.wait_write(cmd=f"local-user {self.username} service-type ssh terminal telnet ftp", wait="]")
        self.wait_write(cmd=f"local-user {self.username} level 3", wait="]")

        self.wait_write(cmd=f"authentication-scheme default_admin", wait="]")
        self.wait_write(cmd=f"authentication-mode local hwtacacs", wait="]")
        self.wait_write(cmd="quit", wait="]")
        self.wait_write(cmd=f"authorization-scheme default_admin", wait="]")
        self.wait_write(cmd=f"authorization-mode local hwtacacs", wait="]")
        self.wait_write(cmd="quit", wait="]")
        self.wait_write(cmd="quit", wait="]")

        # Commit is hanging in the bootstrap with version R23 for unknown reason so we rely on the auto-commit with mmi-mode
        #self.wait_write(cmd="commit", wait="]")
        time.sleep(5)

        #self.wait_write(
        #    cmd=f"local-user {self.username} user-group manage-ug", wait="]"
        #)
        #self.wait_write(cmd="quit", wait="]")

        # VTY configuration
        self.wait_write(cmd="user-interface vty 0 4", wait="]")
        self.wait_write(cmd="authentication-mode aaa", wait="]")
        # We want all protocols to be allowed on the vty
        self.wait_write(cmd="protocol inbound all", wait="]")
        # We want only ssh to be allowed on the vty
        #self.wait_write(cmd="protocol inbound ssh", wait="]")
        self.wait_write(cmd="quit", wait="]")
        
        # Commit is hanging in the bootstrap with version R23 for unknown reason so we rely on the auto-commit with mmi-mode
        #self.wait_write(cmd="commit", wait="]")
        time.sleep(5)

        # Enable stelnet, sftp, scp, ssh
        self.wait_write(cmd="stelnet server enable", wait="]")
        self.wait_write(cmd="sftp ipv4 server enable", wait="]")
        self.wait_write(cmd="scp server enable", wait="]")
        self.logger.info("Skipping advanced SSH/SFTP settings for AR1000V")


        # NETCONF seems to crash the virtual R23 router so do not configure it
        #self.wait_write(cmd="snetconf server enable", wait="]")
        #self.wait_write(cmd="netconf", wait="]")
        #self.wait_write(cmd="protocol inbound ssh port 830", wait="]")
        #self.wait_write(cmd="quit", wait="]")

        time.sleep(5)
        # We will only do a final quit here and with mmi-mode enable all changes will be commited automatically
        self.wait_write(cmd="quit", wait="]")
        # if we do not commit we will not see the ">", with mmi-mode enable the system automatically commits when leaving system-view
        #self.wait_write(cmd="save", wait=">")
        # Under heavy load commit might take some seconds. Better give some more time to wait for commit to complete.
        time.sleep(15)
        #self.wait_write(cmd="undo mmi-mode enable", wait=">")


    def startup_config(self):
        if not os.path.exists(STARTUP_CONFIG_FILE):
            self.logger.trace(f"Startup config file {STARTUP_CONFIG_FILE} not found")
            return
        

        vrnetlab.run_command(["cp", STARTUP_CONFIG_FILE, "/tftpboot/containerlab.cfg"])


        self.bootstrap_mgmt_interface()
        #self.wait_write(cmd="commit", wait="]")


        self.wait_write(cmd=f"return", wait="]")
        time.sleep(1)
        self.wait_write(cmd=f"tftp 10.0.0.2 vpn-instance __MGMT_VPN__ get containerlab.cfg", wait=">")
        self.wait_write(cmd="startup saved-configuration containerlab.cfg", wait=">")
        self.wait_write(cmd="reboot fast", wait=">")
        self.wait_write(cmd="reboot", wait="#")
        self.wait_write(cmd="", wait="The current login time is")
        print(f"File '{STARTUP_CONFIG_FILE}' successfully loaded")

    def gen_mgmt(self):
        """Generate qemu args for the mgmt interface(s)"""
        # call parent function to generate the mgmt interface
        res = super().gen_mgmt()

        # Creates required dummy interface
        res.append(f"-device virtio-net-pci,netdev=dummy,mac={vrnetlab.gen_mac(0)}")
        res.append("-netdev tap,ifname=vrp-dummy,id=dummy,script=no,downscript=no")

        return res


class AR1000V(vrnetlab.VR):
    def __init__(self, hostname, username, password, conn_mode):
        super(AR1000V, self).__init__(username, password)
        self.vms = [AR1000V_vm(username, password, hostname, conn_mode)]


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="")
    parser.add_argument(
        "--trace", action="store_true", help="enable trace level logging"
    )
    parser.add_argument("--hostname", default="vr-AR1000V", help="Router hostname")
    parser.add_argument("--username", default="vrnetlab", help="Username")
    parser.add_argument("--password", default="VR-netlab9", help="Password")
    parser.add_argument(
        "--connection-mode",
        default="tc",
        help="Connection mode to use in the datapath",
    )

    args = parser.parse_args()

    LOG_FORMAT = "%(asctime)s: %(module)-10s %(levelname)-8s %(message)s"
    logging.basicConfig(format=LOG_FORMAT)
    logger = logging.getLogger()

    logger.setLevel(logging.DEBUG)

    if args.trace:
        logger.setLevel(1)

    vr = AR1000V(
        args.hostname, args.username, args.password, conn_mode=args.connection_mode
    )
    vr.start()
