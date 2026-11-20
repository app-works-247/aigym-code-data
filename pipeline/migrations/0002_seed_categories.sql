-- 0002_seed_categories.sql: the 9 Radar categories (PLAN.md section 6.2).
-- lesson_unit = related course level; NULL means "further reading".
-- sort_order follows the filter chips in the approved app design.

INSERT INTO categories (slug, title_vi, title_en, lesson_unit, sort_order) VALUES
  ('skills',    'Skills',               'Skills',                   3, 1),
  ('mcp',       'MCP server',           'MCP servers',              5, 2),
  ('subagents', 'Subagent',             'Subagents',                4, 3),
  ('hooks',     'Hook',                 'Hooks',                    6, 4),
  ('plugins',   'Plugin & marketplace', 'Plugins & marketplaces',   8, 5),
  ('commands',  'Lệnh slash',           'Slash commands',           3, 6),
  ('templates', 'CLAUDE.md & mẫu',      'CLAUDE.md & templates',    2, 7),
  ('tools',     'Công cụ & giao diện',  'Tools & GUIs',          NULL, 8),
  ('learn',     'Học thêm',             'Learning & awesome lists', NULL, 9);
