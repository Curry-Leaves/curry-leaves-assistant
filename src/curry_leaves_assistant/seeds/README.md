# Bundled seeds

Source of truth for everything the app writes into a fresh `~/.curry-leaves/` on
first startup. Each file here is directly editable — change a default agent or
skill by editing its markdown, no Python involved.

- `agents/<id>.md` — one file per default agent, same format as a live agent file
  (`~/.curry-leaves/agents/<id>.md`): YAML frontmatter + instructions body. The
  operational fields that the runtime keeps in `<id>.meta.json` (`surfaces`,
  `triggers`, `schedule`, `subagents`, `internal`, `always`) are written inline
  here; the seeder splits them out on first write. The filename stem is the agent
  id. Seeded by `stores/agent_store.seed_default_agents()`.
- `skills/<name>/` — one directory per default skill, copied verbatim into
  `~/.curry-leaves/skills/<name>/` (SKILL.md plus any starter files, e.g.
  skill-learner's `index.md`). Seeded by `stores/skills_store.seed_default_skills()`.

Seeding is once-only per item: anything already on disk — including user edits —
is never touched. To adopt a changed seed on a running install, delete that agent
or skill and restart.
