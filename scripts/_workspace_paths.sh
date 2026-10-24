#!/bin/bash

get_script_dir() {
    local source_path="${BASH_SOURCE[0]}"
    cd "$(dirname "$source_path")" && pwd
}

get_project_root() {
    local script_dir="${1:-$(get_script_dir)}"
    cd "$script_dir/.." && pwd
}

get_shared_workspace_root() {
    local project_root="${1:-$(get_project_root)}"
    local git_file="$project_root/.git"
    if [ -f "$git_file" ]; then
        local gitdir_line
        gitdir_line="$(tr -d '\n' < "$git_file")"
        case "$gitdir_line" in
            gitdir:\ *)
                local gitdir_path="${gitdir_line#gitdir: }"
                case "$gitdir_path" in
                    /*) ;;
                    *) gitdir_path="$project_root/$gitdir_path" ;;
                esac
                gitdir_path="$(cd "$gitdir_path" && pwd)"
                if [ "$(basename "$gitdir_path")" = ".git" ]; then
                    dirname "$gitdir_path"
                    return 0
                fi
                if [ "$(basename "$(dirname "$gitdir_path")")" = ".git" ]; then
                    dirname "$(dirname "$gitdir_path")"
                    return 0
                fi
                if [ "$(basename "$(dirname "$(dirname "$gitdir_path")")")" = ".git" ]; then
                    dirname "$(dirname "$(dirname "$gitdir_path")")"
                    return 0
                fi
                ;;
        esac
    fi
    printf '%s\n' "$project_root"
}

get_shared_sglang_activate() {
    local project_root="${1:-$(get_project_root)}"
    local shared_root
    shared_root="$(get_shared_workspace_root "$project_root")"
    printf '%s/.sglang/bin/activate\n' "$shared_root"
}

get_shared_sglang_python() {
    local project_root="${1:-$(get_project_root)}"
    local shared_root
    shared_root="$(get_shared_workspace_root "$project_root")"
    printf '%s/.sglang/bin/python\n' "$shared_root"
}

get_shared_paddle_ocr_python() {
    local project_root="${1:-$(get_project_root)}"
    local shared_root
    shared_root="$(get_shared_workspace_root "$project_root")"
    printf '%s/.paddle-ocr/bin/python\n' "$shared_root"
}

get_shared_paddle_ocr_activate() {
    local project_root="${1:-$(get_project_root)}"
    local shared_root
    shared_root="$(get_shared_workspace_root "$project_root")"
    printf '%s/.paddle-ocr/bin/activate\n' "$shared_root"
}
