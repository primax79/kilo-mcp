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

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PLUGIN_SKILL_MAPPINGS = [
    ('plugins/kilo-mcp', 'skills'),
]

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

if __name__ == "__main__":
    generate_indices()
