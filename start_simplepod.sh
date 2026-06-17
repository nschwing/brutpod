#!/usr/bin/env bash

python deploy_simplepod.py \
    --api-key YOUR_SIMPLEPOD_API_KEY \
    --gpu-model "RTX 5090" \
    --template YOUR_TEMPLATE_ID \
    --cuda 13.0 \
    --proxy http://localhost:8118
