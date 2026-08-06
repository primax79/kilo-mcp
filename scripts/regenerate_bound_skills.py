#!/usr/bin/env python3
"""regenerate_bound_skills.py - regenerates this repo's bound skills from
ai-architect-executor's generic templates + this repo's own bindings/*.json.

This is the consumer side of the "generate, don't duplicate" design:
ai-architect-executor owns the templates and the generator script and does
not know kilo-mcp exists; kilo-mcp owns its own binding maps (one per
generic skill it binds) and points the generic repo's generator at them.
That keeps the dependency one-directional, matching the declared
plugin.json dependency (kilo-mcp -> ai-architect-executor).

Each bindings/<skill>.json must correspond to a template at
<ai-architect-executor>/plugins/architect-executor/skills/<skill>/SKILL.template.md.
The map's own "name" field gives the concrete skill's directory name under
plugins/kilo-mcp/skills/.

Usage:
  python3 scripts/regenerate_bound_skills.py [--ai-architect-executor PATH] [--check]

  --ai-architect-executor  defaults to ../ai-architect-executor (sibling
                            checkout), or $AI_ARCHITECT_EXECUTOR_PATH if set
  --check                  don't write; verify every bound skill matches its
                            template + map and exit non-zero on drift
                            (same semantics as generate_binding.py --check)
"""
import argparse
import json
import os
import pathlib
import subprocess
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
BINDINGS_DIR = REPO_ROOT / 'bindings'
SKILLS_OUT_DIR = REPO_ROOT / 'plugins' / 'kilo-mcp' / 'skills'


def find_ai_architect_executor(explicit):
    if explicit:
        return pathlib.Path(explicit).resolve()
    env = os.environ.get('AI_ARCHITECT_EXECUTOR_PATH')
    if env:
        return pathlib.Path(env).resolve()
    sibling = (REPO_ROOT / '..' / 'ai-architect-executor').resolve()
    return sibling


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--ai-architect-executor', default=None,
                     help='Path to the ai-architect-executor checkout (default: sibling dir, or $AI_ARCHITECT_EXECUTOR_PATH)')
    ap.add_argument('--check', action='store_true')
    args = ap.parse_args()

    aae_root = find_ai_architect_executor(args.ai_architect_executor)
    generator = aae_root / 'scripts' / 'generate_binding.py'
    if not generator.is_file():
        print(f"[FAIL] generator not found at {generator} "
              f"(pass --ai-architect-executor or set $AI_ARCHITECT_EXECUTOR_PATH)", file=sys.stderr)
        sys.exit(2)

    if not BINDINGS_DIR.is_dir():
        print(f"[FAIL] no bindings/ dir at {BINDINGS_DIR}", file=sys.stderr)
        sys.exit(2)

    failures = 0
    ran = 0
    for map_path in sorted(BINDINGS_DIR.glob('*.json')):
        skill_id = map_path.stem  # generic skill name, e.g. "orchestration-methodology"
        template = aae_root / 'plugins' / 'architect-executor' / 'skills' / skill_id / 'SKILL.template.md'
        if not template.is_file():
            print(f"[FAIL] {map_path.name}: no template at {template}")
            failures += 1
            continue

        concrete_name = json.loads(map_path.read_text())['name']
        out_path = SKILLS_OUT_DIR / concrete_name / 'SKILL.md'

        cmd = [sys.executable, str(generator), '--template', str(template),
               '--map', str(map_path), '--out', str(out_path)]
        if args.check:
            cmd.append('--check')

        result = subprocess.run(cmd)
        ran += 1
        if result.returncode != 0:
            failures += 1

    verb = 'Checked' if args.check else 'Regenerated'
    print(f"\n{verb} {ran} bound skill(s), {failures} failure(s).")
    sys.exit(1 if failures else 0)


if __name__ == '__main__':
    main()
