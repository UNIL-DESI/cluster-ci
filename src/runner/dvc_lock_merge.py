import os
import sys
from pathlib import Path

if sys.platform.startswith("win"):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except AttributeError:
        pass

try:
    from ruamel.yaml import YAML
    _HAS_RUAMEL = True
except ImportError:
    _HAS_RUAMEL = False
    try:
        import yaml as pyyaml
    except ImportError:
        print("❌ Error: neither 'ruamel.yaml' nor 'pyyaml' is available.", file=sys.stderr)
        sys.exit(1)


class MergeConflictError(Exception):
    """Exception raised when a 3-way merge conflict cannot be automatically resolved."""
    pass


def _to_plain(obj):
    """Recursively convert YAML data structures (e.g. CommentedMap/Seq) to plain Python types."""
    if isinstance(obj, dict):
        return {k: _to_plain(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_to_plain(item) for item in obj]
    return obj


def _get_yaml_instance():
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.indent(mapping=2, sequence=2, offset=0)
    return yaml


def merge_dvc_lock_data(o_data, a_data, b_data):
    """Perform a 3-way merge on parsed dvc.lock data structures.
    
    o_data: Base / common ancestor
    a_data: Ours / current branch
    b_data: Theirs / incoming branch
    
    Returns the merged dict structure, or raises MergeConflictError on unresolvable conflicts.
    """
    o_data = o_data or {}
    a_data = a_data or {}
    b_data = b_data or {}

    o_stages = o_data.get('stages') or {}
    a_stages = a_data.get('stages') or {}
    b_stages = b_data.get('stages') or {}

    if not isinstance(o_stages, dict) or not isinstance(a_stages, dict) or not isinstance(b_stages, dict):
        raise MergeConflictError("Invalid 'stages' section: expected a dictionary of stages.")

    merged_stages = {}

    # 1. Process stages present in Ours (A)
    for name, a_val in a_stages.items():
        a_plain = _to_plain(a_val)
        in_o = name in o_stages

        if in_o:
            o_val = o_stages[name]
            o_plain = _to_plain(o_val)
            a_changed = (a_plain != o_plain)

            if name in b_stages:
                b_val = b_stages[name]
                b_plain = _to_plain(b_val)
                b_changed = (b_plain != o_plain)

                if not a_changed and not b_changed:
                    # Unchanged on both sides
                    merged_stages[name] = a_val
                elif a_changed and not b_changed:
                    # Modified only in Ours (A)
                    merged_stages[name] = a_val
                elif not a_changed and b_changed:
                    # Modified only in Theirs (B)
                    merged_stages[name] = b_val
                else:
                    # Modified in both A and B
                    if a_plain == b_plain:
                        # Identical modifications
                        merged_stages[name] = a_val
                    else:
                        raise MergeConflictError(
                            f"Conflict on stage '{name}': modified differently in both branches.\n"
                            f"  Ours:   {a_plain}\n"
                            f"  Theirs: {b_plain}"
                        )
            else:
                # Deleted in Theirs (B)
                if not a_changed:
                    # Unchanged in A, deleted in B -> delete
                    pass
                else:
                    # Modified in A, deleted in B -> conflict
                    raise MergeConflictError(
                        f"Conflict on stage '{name}': modified in our branch but deleted in other branch."
                    )
        else:
            # Added in Ours (A)
            if name in b_stages:
                # Added in both A and B
                b_val = b_stages[name]
                b_plain = _to_plain(b_val)
                if a_plain == b_plain:
                    merged_stages[name] = a_val
                else:
                    raise MergeConflictError(
                        f"Conflict on stage '{name}': added with different content in both branches.\n"
                        f"  Ours:   {a_plain}\n"
                        f"  Theirs: {b_plain}"
                    )
            else:
                # Added only in Ours (A)
                merged_stages[name] = a_val

    # 2. Process stages present in Theirs (B) but not in Ours (A)
    # Deterministic stage order: Order of A, then additions of B
    for name, b_val in b_stages.items():
        if name in merged_stages or name in a_stages:
            continue

        b_plain = _to_plain(b_val)
        in_o = name in o_stages

        if in_o:
            # Existed in Base (O), but deleted in Ours (A)
            o_val = o_stages[name]
            o_plain = _to_plain(o_val)
            b_changed = (b_plain != o_plain)
            if not b_changed:
                # Deleted in A, unchanged in B -> delete
                pass
            else:
                # Deleted in A, modified in B -> conflict
                raise MergeConflictError(
                    f"Conflict on stage '{name}': deleted in our branch but modified in other branch."
                )
        else:
            # Added only in Theirs (B) -> keep B
            merged_stages[name] = b_val

    # 3. Process top-level keys (e.g. schema: '2.0')
    merged_root = {}
    all_root_keys = list(a_data.keys()) + [k for k in b_data.keys() if k not in a_data]
    for k in all_root_keys:
        if k == 'stages':
            continue

        a_has = k in a_data
        b_has = k in b_data
        o_has = k in o_data

        a_val = a_data.get(k)
        b_val = b_data.get(k)
        o_val = o_data.get(k)

        a_plain = _to_plain(a_val) if a_has else None
        b_plain = _to_plain(b_val) if b_has else None
        o_plain = _to_plain(o_val) if o_has else None

        if o_has:
            if a_has and b_has:
                a_mod = (a_plain != o_plain)
                b_mod = (b_plain != o_plain)
                if not a_mod and not b_mod:
                    merged_root[k] = a_val
                elif a_mod and not b_mod:
                    merged_root[k] = a_val
                elif not a_mod and b_mod:
                    merged_root[k] = b_val
                else:
                    if a_plain == b_plain:
                        merged_root[k] = a_val
                    else:
                        raise MergeConflictError(
                            f"Conflict on top-level key '{k}': modified differently in both branches."
                        )
            elif a_has and not b_has:
                if a_plain == o_plain:
                    pass  # Deleted in B
                else:
                    raise MergeConflictError(
                        f"Conflict on top-level key '{k}': modified in our branch but deleted in other branch."
                    )
            elif not a_has and b_has:
                if b_plain == o_plain:
                    pass  # Deleted in A
                else:
                    raise MergeConflictError(
                        f"Conflict on top-level key '{k}': deleted in our branch but modified in other branch."
                    )
        else:
            if a_has and b_has:
                if a_plain == b_plain:
                    merged_root[k] = a_val
                else:
                    raise MergeConflictError(
                        f"Conflict on top-level key '{k}': added differently in both branches."
                    )
            elif a_has:
                merged_root[k] = a_val
            elif b_has:
                merged_root[k] = b_val

    # Assemble final document preserving canonical order (schema, then stages, then others)
    final_doc = {}
    if 'schema' in merged_root:
        final_doc['schema'] = merged_root['schema']
    elif 'schema' in a_data:
        final_doc['schema'] = a_data['schema']
    elif 'schema' in b_data:
        final_doc['schema'] = b_data['schema']

    final_doc['stages'] = merged_stages

    for k, v in merged_root.items():
        if k not in ('schema', 'stages'):
            final_doc[k] = v

    return final_doc


def merge_dvc_lock_files(path_o, path_a, path_b):
    """Read Base (%O), Ours (%A), Theirs (%B) files, merge, and overwrite Ours (%A)."""
    if _HAS_RUAMEL:
        yaml = _get_yaml_instance()

        def _read_file(path):
            if not os.path.exists(path) or os.path.getsize(path) == 0:
                return {}
            with open(path, 'r', encoding='utf-8') as f:
                data = yaml.load(f)
                return data or {}

        o_data = _read_file(path_o)
        a_data = _read_file(path_a)
        b_data = _read_file(path_b)

        merged_data = merge_dvc_lock_data(o_data, a_data, b_data)

        with open(path_a, 'w', encoding='utf-8') as f:
            yaml.dump(merged_data, f)
    else:
        def _read_file(path):
            if not os.path.exists(path) or os.path.getsize(path) == 0:
                return {}
            with open(path, 'r', encoding='utf-8') as f:
                data = pyyaml.safe_load(f)
                return data or {}

        o_data = _read_file(path_o)
        a_data = _read_file(path_a)
        b_data = _read_file(path_b)

        merged_data = merge_dvc_lock_data(o_data, a_data, b_data)

        with open(path_a, 'w', encoding='utf-8') as f:
            pyyaml.dump(merged_data, f, sort_keys=False, default_flow_style=False, indent=2)


def main():
    if len(sys.argv) < 4:
        print("Usage: python -m src.runner.dvc_lock_merge <base/%O> <ours/%A> <theirs/%B>", file=sys.stderr)
        sys.exit(2)

    path_o, path_a, path_b = sys.argv[1], sys.argv[2], sys.argv[3]
    try:
        merge_dvc_lock_files(path_o, path_a, path_b)
        print(f"ℹ️  [DVC-Lock-Merge] Successfully merged dvc.lock into {path_a}")
        sys.exit(0)
    except MergeConflictError as e:
        print(f"❌ [DVC-Lock-Merge] Merge conflict: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"❌ [DVC-Lock-Merge] Unexpected error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
