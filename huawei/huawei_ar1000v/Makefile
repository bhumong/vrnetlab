VENDOR=Huawei
NAME=AR1000V
IMAGE_FORMAT=qcow2
IMAGE_GLOB=*.qcow2

# match versions like:
# huawei_ar1k-<any version string>.qcow2
# huawei_ar1000v-<any version string>.qcow2
VERSION=$(shell echo $(IMAGE) | sed -e 's/huawei_\(ar1k\|ar1000v\)-\(.*\)\.qcow2/\2/')

-include ../../makefile-sanity.include
-include ../../makefile.include
