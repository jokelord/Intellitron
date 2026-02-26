#!/bin/bash

args=()
ptx_file=""

for arg in "$@"; do
    case $arg in
        --gpu-name=sm_110)
            args+=("--gpu-name=sm_110a")
            ;;
        --gpu-name=sm_11.0)
            args+=("--gpu-name=sm_11.0a")
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

if [[ -n "$ptx_file" ]]; then
    sed -i 's/sm_110/sm_110a/g' "$ptx_file"
    sed -i 's/sm_11\.0/sm_11.0a/g' "$ptx_file"
fi

#!/bin/bash

args=()
ptx_file=""

for arg in "$@"; do
    case $arg in
        --gpu-name=sm_110)
            args+=("--gpu-name=sm_110a")
            ;;
        --gpu-name=sm_11.0)
            args+=("--gpu-name=sm_11.0a")
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

if [[ -n "$ptx_file" ]]; then
    sed -i 's/sm_110/sm_110a/g' "$ptx_file"
    sed -i 's/sm_11\.0/sm_11.0a/g' "$ptx_file"
fi

exec /usr/local/cuda-13.1/bin/ptxas "${args[@]}"
