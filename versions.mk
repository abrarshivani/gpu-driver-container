# Copyright (c) 2022, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# DRIVER_VERSIONS contains latest version in all active datacenter branches
# NOTE: the ubuntu2604 CUDA repository does not carry branch 580, so the aggregate
# ubuntu26.04 targets cannot build it; use per-version targets (e.g.
# build-ubuntu26.04-595.71.05) or override DRIVER_VERSIONS for that distribution.
DRIVER_VERSIONS ?= 580.173.02 595.71.05

GOLANG_VERSION := 1.26.4

GIT_COMMIT ?= $(shell git describe --match="" --dirty --long --always --abbrev=40 2> /dev/null || echo "")
