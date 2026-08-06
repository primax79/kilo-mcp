#!/usr/bin/env python3
"""
generate_skill_indices.py - Generates Kilo-compatible index.json files at:
1. Plugin Level (e.g. plugins/kilo-mcp/index.json)
2. Skills Directory Level (e.g. plugins/kilo-mcp/skills/index.json)
3. Individual Skill Level (e.g. plugins/kilo-mcp/skills/mcp-orchestrator/index.json)

Ensures all forms of Kilo 'skills.urls' inputs work seamlessly regardless of
trailing path level (point it at any of the three, Kilo resolves the same set).

Ported from gcube-ai-toolkit/scripts/generate_skill_indices.py, same format.
"""

import os
import json
import subprocess
import sys

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PLUGIN_SKILL_MAPPINGS = [
    ('plugins/kilo-mcp', 'skills'),
]

PROFILES = {'deep-reasoning', 'orchestration', 'bulk-execution', 'exploration'}


def validate_skill_requirements():
    """Kit-internal half of role validation (see ai-architect-executor's
    interactive-role-setup/SKILL.md): every skill declares a valid profile,
    none declares a model. Self-contained per repo on purpose — this generator
    is already a per-repo ported copy, and a cross-repo import would break for
    anyone installing this plugin standalone.
    """
    ok = True
    for rel_plugin_dir, skills_sub in PLUGIN_SKILL_MAPPINGS:
        plugin_dir = os.path.join(ROOT_DIR, rel_plugin_dir)
        skills_dir = os.path.join(plugin_dir, skills_sub)
        if not os.path.isdir(skills_dir):
            continue
        req_path = os.path.join(plugin_dir, 'skill-requirements.json')
        requirements = {}
        if os.path.isfile(req_path):
            with open(req_path, encoding='utf-8') as f:
                requirements = json.load(f)
        for skill_name in sorted(os.listdir(skills_dir)):
            skill_md = os.path.join(skills_dir, skill_name, 'SKILL.md')
            if not os.path.isfile(skill_md):
                continue
            entry = requirements.get(skill_name)
            if entry is None:
                print(f"[roles] FAIL: '{skill_name}' has no skill-requirements.json entry")
                ok = False
                continue
            if entry.get('profile') not in PROFILES:
                print(f"[roles] FAIL: '{skill_name}' has invalid profile {entry.get('profile')!r}")
                ok = False
            if not entry.get('reason', '').strip():
                print(f"[roles] FAIL: '{skill_name}' skill-requirements.json entry has no reason")
                ok = False
            if 'model' in entry:
                print(f"[roles] FAIL: '{skill_name}' skill-requirements.json declares a model — "
                      f"profiles only, never a model")
                ok = False
    return ok

def generate_indices():
    plugin_level_count = 0
    skills_dir_count = 0
    single_skill_count = 0

    for rel_plugin_dir, skills_sub in PLUGIN_SKILL_MAPPINGS:
        abs_plugin_dir = os.path.join(ROOT_DIR, rel_plugin_dir)
        abs_skills_dir = os.path.join(abs_plugin_dir, skills_sub)

        if not os.path.isdir(abs_skills_dir):
            continue

        skills_dir_entries = []
        plugin_dir_entries = []

        for skill_name in sorted(os.listdir(abs_skills_dir)):
            skill_path = os.path.join(abs_skills_dir, skill_name)
            skill_md = os.path.join(skill_path, "SKILL.md")

            if os.path.isdir(skill_path) and os.path.isfile(skill_md):
                files_list = []
                for root, dirs, files in os.walk(skill_path):
                    dirs[:] = [d for d in dirs if not d.startswith(".") and d != "__pycache__"]
                    for f in files:
                        if f.startswith(".") or f.endswith(".pyc") or f == "index.json":
                            continue
                        full_p = os.path.join(root, f)
                        rel_p = os.path.relpath(full_p, skill_path)
                        files_list.append(rel_p)

                sorted_files = sorted(files_list)

                skills_dir_entries.append({
                    "name": skill_name,
                    "files": sorted_files
                })

                plugin_dir_entries.append({
                    "name": f"skills/{skill_name}",
                    "files": sorted_files
                })

                single_skill_index_file = os.path.join(skill_path, "index.json")
                with open(single_skill_index_file, "w", encoding="utf-8") as f:
                    json.dump({"skills": [{"name": skill_name, "files": sorted_files}]}, f, indent=2)
                    f.write("\n")
                single_skill_count += 1

        skills_index_path = os.path.join(abs_skills_dir, "index.json")
        with open(skills_index_path, "w", encoding="utf-8") as f:
            json.dump({"skills": skills_dir_entries}, f, indent=2)
            f.write("\n")
        skills_dir_count += 1

        plugin_index_path = os.path.join(abs_plugin_dir, "index.json")
        with open(plugin_index_path, "w", encoding="utf-8") as f:
            json.dump({"skills": plugin_dir_entries}, f, indent=2)
            f.write("\n")
        plugin_level_count += 1

    print(f"Generated {plugin_level_count} plugin-level, {skills_dir_count} skills-dir-level, and {single_skill_count} individual skill index.json files!")

def check_binding_drift():
    """Every bound skill (SKILL.md generated from ai-architect-executor's
    template + this repo's own bindings/*.json) must match its source —
    see regenerate_bound_skills.py. Skipped with a warning, not a failure,
    when the ai-architect-executor sibling checkout isn't available (exit
    code 2 = setup/environment issue, not drift) — this repo must still
    work standalone for anyone who doesn't have that checkout locally."""
    script = os.path.join(ROOT_DIR, 'scripts', 'regenerate_bound_skills.py')
    if not os.path.isfile(script):
        return True
    result = subprocess.run([sys.executable, script, '--check'])
    if result.returncode == 2:
        print("[warn] skipped binding-drift check: ai-architect-executor checkout not found "
              "(pass --ai-architect-executor or set $AI_ARCHITECT_EXECUTOR_PATH to enable it)")
        return True
    return result.returncode == 0


if __name__ == "__main__":
    generate_indices()
    ok = validate_skill_requirements()
    ok = check_binding_drift() and ok
    if not ok:
        sys.exit(1)
