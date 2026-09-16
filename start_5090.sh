#!/usr/bin/env bash

python deploy_runpod.py \
    --api-key KEY \
    --gpu-type "NVIDIA GeForce RTX 5090" \
    --template i3owfjxo7a \
    --cuda 12.8 \
    --cloud COMMUNITY \
    --volume 0 \
    --container-disk 175 \
    --proxy http://localhost:8118
