-- 0001_init.sql: initial schema for the AIGym Code pipeline.
-- Source of truth: docs/database.md section 3 (approved 2026-11-19).
-- Never edit this file after it has run anywhere; write a new numbered migration instead.

-- ===== Radar =====
CREATE TABLE repos (
  id              BIGINT PRIMARY KEY,                 -- databaseId của GitHub, không đổi khi repo đổi tên
  node_id         TEXT        NOT NULL UNIQUE,        -- id dùng cho GraphQL
  owner           TEXT        NOT NULL,
  name            TEXT        NOT NULL,
  description     TEXT,
  homepage        TEXT,
  language        TEXT,
  license_spdx    TEXT,                               -- NULL = không có license
  topics          TEXT[]      NOT NULL DEFAULT '{}',
  default_branch  TEXT,
  created_at      TIMESTAMPTZ NOT NULL,
  pushed_at       TIMESTAMPTZ,
  archived        BOOLEAN     NOT NULL DEFAULT false,
  is_docs         BOOLEAN     NOT NULL DEFAULT false, -- awesome-list, guide
  is_official     BOOLEAN     NOT NULL DEFAULT false, -- repo của org anthropics
  stars           INTEGER     NOT NULL CHECK (stars >= 0),
  forks           INTEGER     NOT NULL CHECK (forks >= 0),
  open_issues     INTEGER     NOT NULL DEFAULT 0 CHECK (open_issues >= 0),
  owner_country   CHAR(2)     CHECK (owner_country ~ '^[A-Z]{2}$'),
  readme_sha      TEXT,                               -- để biết README đổi thì tóm tắt lại
  first_seen      DATE        NOT NULL DEFAULT CURRENT_DATE,
  last_refreshed  TIMESTAMPTZ,
  status          TEXT        NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'active', 'hidden', 'removed'))
                  -- pending: chờ phân loại · hidden: ẩn tay · removed: owner yêu cầu gỡ
);
-- GitHub không phân biệt hoa thường trong tên repo
CREATE UNIQUE INDEX repos_full_name_uq ON repos (lower(owner), lower(name));
CREATE INDEX repos_active_stars_idx ON repos (stars DESC) WHERE status = 'active';

CREATE TABLE repo_snapshots (
  repo_id  BIGINT  NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
  day      DATE    NOT NULL,
  stars    INTEGER NOT NULL CHECK (stars >= 0),
  forks    INTEGER NOT NULL CHECK (forks >= 0),
  PRIMARY KEY (repo_id, day)                          -- mỗi repo mỗi ngày đúng một dòng
);
CREATE INDEX repo_snapshots_day_idx ON repo_snapshots (day);

CREATE TABLE categories (
  slug         TEXT PRIMARY KEY CHECK (slug ~ '^[a-z][a-z-]*$'),
  title_vi     TEXT     NOT NULL,
  title_en     TEXT     NOT NULL,
  lesson_unit  SMALLINT CHECK (lesson_unit BETWEEN 1 AND 9),   -- cấp bài học liên quan, NULL = "Đọc thêm"
  sort_order   SMALLINT NOT NULL DEFAULT 0
);

CREATE TABLE repo_categories (
  repo_id     BIGINT NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
  slug        TEXT   NOT NULL REFERENCES categories(slug),
  source      TEXT   NOT NULL CHECK (source IN ('tree', 'deps', 'topic', 'llm', 'manual')),
  confidence  REAL   NOT NULL CHECK (confidence BETWEEN 0 AND 1),
  PRIMARY KEY (repo_id, slug)
);
CREATE INDEX repo_categories_slug_idx ON repo_categories (slug);

CREATE TABLE repo_i18n (
  repo_id       BIGINT      NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
  lang          TEXT        NOT NULL CHECK (lang IN ('vi', 'en')),
  summary       TEXT        NOT NULL CHECK (length(summary) BETWEEN 1 AND 600),
  use_when      JSONB       NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(use_when) = 'array'),
  provider      TEXT        NOT NULL,                 -- anthropic | google | openai…
  model         TEXT        NOT NULL,
  source_sha    TEXT        NOT NULL,                 -- readme_sha lúc tạo tóm tắt
  generated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (repo_id, lang)
);

CREATE TABLE repo_installs (
  repo_id  BIGINT   NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
  ord      SMALLINT NOT NULL CHECK (ord >= 0),
  kind     TEXT     NOT NULL CHECK (kind IN ('plugin', 'mcp', 'skill', 'npm', 'pip', 'other')),
  cmd      TEXT     NOT NULL CHECK (length(cmd) BETWEEN 1 AND 500),   -- trích nguyên văn từ README, không do AI viết
  PRIMARY KEY (repo_id, ord)
);

CREATE TABLE repo_flags (
  repo_id  BIGINT NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
  flag     TEXT   NOT NULL CHECK (flag IN ('has_shell_hooks', 'needs_token', 'no_license', 'inactive', 'very_new')),
  PRIMARY KEY (repo_id, flag)
);

-- ===== Release =====
CREATE TABLE releases (
  id            BIGINT PRIMARY KEY,                   -- databaseId của release trên GitHub
  repo_id       BIGINT      NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
  tag           TEXT        NOT NULL,
  name          TEXT,
  url           TEXT        NOT NULL,
  published_at  TIMESTAMPTZ NOT NULL,
  is_breaking   BOOLEAN     NOT NULL DEFAULT false,
  body_sha      TEXT        NOT NULL,                 -- release notes đổi thì tóm tắt lại
  UNIQUE (repo_id, tag)
);
CREATE INDEX releases_published_idx ON releases (published_at DESC);

CREATE TABLE release_i18n (
  release_id    BIGINT      NOT NULL REFERENCES releases(id) ON DELETE CASCADE,
  lang          TEXT        NOT NULL CHECK (lang IN ('vi', 'en')),
  highlights    JSONB       NOT NULL CHECK (jsonb_typeof(highlights) = 'array'),
  check_passed  BOOLEAN     NOT NULL,                 -- qua lưới an toàn chưa (PLAN.md 8.2)
  provider      TEXT        NOT NULL,
  model         TEXT        NOT NULL,
  generated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (release_id, lang)
);

-- ===== Tin tức =====
CREATE TABLE source_state (
  source_id         TEXT PRIMARY KEY,                 -- khớp id trong sources.yaml
  last_ok_at        TIMESTAMPTZ,
  last_error        TEXT,
  last_error_at     TIMESTAMPTZ,
  empty_parse_count INTEGER NOT NULL DEFAULT 0 CHECK (empty_parse_count >= 0)
);

CREATE TABLE news_items (
  id            TEXT PRIMARY KEY CHECK (id ~ '^[0-9a-f]{12}$'),  -- 12 ký tự đầu hash của URL chuẩn hóa; dùng trong đường dẫn JSON
  source_id     TEXT        NOT NULL REFERENCES source_state(source_id),
  url           TEXT        NOT NULL UNIQUE,          -- URL đã chuẩn hóa
  dedup_key     TEXT        UNIQUE,                   -- ví dụ 'claude-code@2.1.328': gộp CHANGELOG và GitHub Release thành một tin
  title_orig    TEXT        NOT NULL,
  excerpt       TEXT        NOT NULL CHECK (length(excerpt) <= 8000),  -- đoạn trích để tóm tắt và đối chiếu; không xuất ra JSON
  published_at  TIMESTAMPTZ NOT NULL,
  fetched_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  tags          TEXT[]      NOT NULL DEFAULT '{}',
  importance    SMALLINT    CHECK (importance BETWEEN 0 AND 3),
  lesson_ids    TEXT[]      NOT NULL DEFAULT '{}',
  status        TEXT        NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'published', 'title_only', 'hidden'))
                -- title_only: tóm tắt không qua lưới an toàn hoặc bị báo sai, chỉ hiện tiêu đề gốc + link
);
CREATE INDEX news_items_feed_idx ON news_items (published_at DESC)
  WHERE status IN ('published', 'title_only');

CREATE TABLE news_i18n (
  news_id       TEXT        NOT NULL REFERENCES news_items(id) ON DELETE CASCADE,
  lang          TEXT        NOT NULL CHECK (lang IN ('vi', 'en')),
  title         TEXT        NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
  summary       TEXT        NOT NULL CHECK (length(summary) BETWEEN 1 AND 800),
  check_passed  BOOLEAN     NOT NULL,
  provider      TEXT        NOT NULL,
  model         TEXT        NOT NULL,
  generated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (news_id, lang)
);

CREATE TABLE news_reports (
  news_id      TEXT        NOT NULL REFERENCES news_items(id) ON DELETE CASCADE,
  channel      TEXT        NOT NULL CHECK (channel IN ('github_issue', 'email')),
  ref          TEXT        NOT NULL,                  -- số issue, hoặc mã do chủ sở hữu ghi khi xử lý email
  reported_at  TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (news_id, channel, ref)
);

-- ===== Docs và vận hành =====
CREATE TABLE doc_pages (
  url          TEXT PRIMARY KEY,
  sha          TEXT        NOT NULL,
  fetched_at   TIMESTAMPTZ NOT NULL,
  changed_at   TIMESTAMPTZ NOT NULL,
  last_pr_url  TEXT                                   -- PR tra cứu gần nhất do docs_watch mở
);

CREATE TABLE pipeline_runs (
  id           BIGSERIAL PRIMARY KEY,
  job          TEXT        NOT NULL CHECK (job IN ('nightly', 'official', 'weekly', 'upstream')),
  started_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at  TIMESTAMPTZ,
  status       TEXT        NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'ok', 'failed')),
  stats        JSONB       NOT NULL DEFAULT '{}',     -- số repo cập nhật, số tin mới, lỗi…
  CHECK (finished_at IS NULL OR finished_at >= started_at)
);
CREATE INDEX pipeline_runs_job_idx ON pipeline_runs (job, started_at DESC);
