#!/bin/bash

args=()
ptx_file=""
gpu_name=""

for arg in "$@"; do
    case $arg in
        --gpu-name=*)
            gpu_name="${arg#--gpu-name=}"
            args+=("$arg")
            ;;
        *.ptx)
            ptx_file="$arg"
            args+=("$arg")
            ;;
        *)
            args+=("$arg")
            ;;
    esac
done

# Determine which ptxas to use
if [[ "$gpu_name" == sm_110* ]]; then
    ptxas_path="/usr/local/cuda-13.1/bin/ptxas"
    # For sm_110, keep sm_110a
    sed_pattern='s/sm_110/sm_110a/g'
    sed_pattern2='s/sm_11\.0/sm_11.0a/g'
else
    ptxas_path="/usr/local/cuda-13.1/bin/ptxas"
    # For others, use sm_110a if needed
    sed_pattern='s/sm_110/sm_110a/g'
    sed_pattern2='s/sm_11\.0/sm_11.0a/g'
fi

if [[ -n "$ptx_file" ]]; then
    sed -i "$sed_pattern" "$ptx_file"
    sed -i "$sed_pattern2" "$ptx_file"
fi

exec "$ptxas_path" "${args[@]}"
