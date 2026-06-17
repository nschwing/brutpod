#!/usr/bin/env bash

python deploy_runpod.py \
    --api-key KEY \
    --gpu-types "NVIDIA GeForce RTX 4090" \
    --template sp4jgeficx \
    --cuda 12.8 \
    --cloud SECURE \
    --volume 0 \
    --container-disk 175 \
    --proxy http://localhost:8118
