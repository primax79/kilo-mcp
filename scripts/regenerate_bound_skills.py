#!/usr/bin/env python3
"""regenerate_bound_skills.py - regenerates this repo's bound skills from
ai-orchestration's generic templates + this repo's own bindings/*.json.

This is the consumer side of the "generate, don't duplicate" design:
ai-orchestration owns the templates and the generator script and does
not know kilo-mcp exists; kilo-mcp owns its own binding maps (one per
generic skill it binds) and points the generic repo's generator at them.
That keeps the dependency one-directional, matching the declared
plugin.json dependency (kilo-mcp -> ai-orchestration).

Each bindings/<skill>.json must correspond to a template at
<ai-orchestration>/plugins/*/skills/<skill>/SKILL.template.md (searched
across all of that repo's plugins - it doesn't matter which one the generic
skill lives in). The map's own "name" field gives the concrete skill's
directory name, and its "plugin" field (architect-side/executor-side) says
which of this repo's own plugins it's written into.

Usage:
  python3 scripts/regenerate_bound_skills.py [--ai-orchestration PATH] [--check]

  --ai-orchestration        defaults to ../ai-orchestration (sibling
                            checkout, falling back to ../ai-architect-executor
                            if ../ai-orchestration does not exist), or
                            $AI_ORCHESTRATION_PATH / $AI_ARCHITECT_EXECUTOR_PATH
  --check                   don't write; verify every bound skill matches its
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
PLUGINS_DIR = REPO_ROOT / 'plugins'
DEFAULT_UPSTREAM = (REPO_ROOT / '..' / 'ai-orchestration').resolve()

BINDINGS = (
    ('orchestration-workflow', 'plugins/architect-side/skills/kilo-mcp-orchestrator/SKILL.md'),
    ('orchestration-metrics', 'plugins/architect-side/skills/kilo-mcp-metrics-analyst/SKILL.md'),
    ('conflict-resolver', 'plugins/architect-side/skills/kilo-mcp-conflict-resolver/SKILL.md'),
    ('executor-contract', 'plugins/executor-side/skills/kilo-mcp-executor-contract/SKILL.md'),
)

TEMPLATE_ALIASES = {
    'orchestration-workflow': ('orchestration-workflow', 'orchestration-methodology'),
    'orchestration-metrics': ('orchestration-metrics', 'delegation-roi-analysis'),
    'conflict-resolver': ('conflict-resolver',),
    'executor-contract': ('executor-contract', 'headless-executor-contract'),
}


def find_ai_architect_executor(explicit=None):
    if explicit:
        return pathlib.Path(explicit).resolve()
    env = os.environ.get('AI_ORCHESTRATION_PATH') or os.environ.get('AI_ARCHITECT_EXECUTOR_PATH')
    if env:
        return pathlib.Path(env).resolve()
    if DEFAULT_UPSTREAM.exists():
        return DEFAULT_UPSTREAM
    cur = REPO_ROOT
    while cur.parent != cur:
        candidate = cur.parent / 'ai-orchestration'
        if candidate.is_dir():
            return candidate.resolve()
        cur = cur.parent
    return (REPO_ROOT / '..' / 'ai-architect-executor').resolve()


def find_template(aae_root, skill_id):
    for candidate in TEMPLATE_ALIASES.get(skill_id, (skill_id,)):
        matches = list(aae_root.glob(f'plugins/*/skills/{candidate}/SKILL.template.md'))
        if matches:
            return matches[0]
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--ai-orchestration', '--ai-architect-executor', dest='ai_architect_executor', default=None,
                     help='Path to the ai-orchestration checkout (default: ../ai-orchestration, or $AI_ORCHESTRATION_PATH)')
    ap.add_argument('--check', action='store_true')
    args = ap.parse_args()

    aae_root = find_ai_architect_executor(args.ai_architect_executor)
    generator = aae_root / 'scripts' / 'generate_binding.py'
    if not generator.is_file():
        print(f"[FAIL] generator not found at {generator} "
              f"(pass --ai-orchestration or set $AI_ORCHESTRATION_PATH)", file=sys.stderr)
        sys.exit(2)

    if not BINDINGS_DIR.is_dir():
        print(f"[FAIL] no bindings/ dir at {BINDINGS_DIR}", file=sys.stderr)
        sys.exit(2)

    failures = 0
    ran = 0
    for map_path in sorted(BINDINGS_DIR.glob('*.json')):
        skill_id = map_path.stem  # generic skill name, e.g. "orchestration-workflow"
        template = find_template(aae_root, skill_id)
        if not template:
            print(f"[FAIL] {map_path.name}: no SKILL.template.md for '{skill_id}' "
                  f"found under {aae_root / 'plugins'}/*/skills/")
            failures += 1
            continue

        binding = json.loads(map_path.read_text())
        concrete_name = binding['name']
        plugin = binding.get('plugin')
        if not plugin:
            print(f"[FAIL] {map_path.name}: no \"plugin\" field - which of this repo's "
                  f"plugins does {concrete_name} belong to?")
            failures += 1
            continue
        out_path = PLUGINS_DIR / plugin / 'skills' / concrete_name / 'SKILL.md'

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
