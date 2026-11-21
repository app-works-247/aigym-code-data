# Thiết kế database: AIGym Code (giai đoạn 3)

> Trạng thái: ✅ **ĐÃ DUYỆT** (2026-11-19) · Lập ngày 2026-11-19 · Áp dụng cho repo `aigym-code-data` (pipeline).
> **Đã triển khai trên Neon 2026-11-20:** project `aigym-code` (`green-snow-92815253`), AWS us-east-1, **Postgres 18**, nhánh `production`, database `neondb`; đã chạy migration 0001–0003.
> Khi được duyệt, phần SQL ở mục 3 sẽ trở thành file `pipeline/migrations/0001_init.sql`.

## 1. Database này dùng để làm gì

- **Chỉ pipeline dùng database.** App iOS không kết nối database. App chỉ tải các file JSON tĩnh mà pipeline xuất ra (PLAN.md mục 6.9).
- Database giữ những thứ cần nhớ giữa các lần chạy:
  - danh sách repo và số star mỗi ngày (để tính +1d / +7d / +30d);
  - tin và release đã lấy (để không lấy trùng);
  - các bản tóm tắt AI đã tạo (để không phải trả tiền tạo lại);
  - dấu vết các trang docs (để biết trang nào vừa đổi).
- Những thứ **không** nằm trong database:
  - bài học, mục tra cứu, mẫu cấu hình, bảng model: là file trong repo (`content/`), sửa qua PR;
  - cấu hình nguồn tin: `pipeline/sources.yaml`;
  - trạng thái dịch vụ: lấy trực tiếp từ status.claude.com (PLAN.md mục 12.3), không lưu.
- **Không có dữ liệu cá nhân của người dùng app.** Chỉ có dữ liệu công khai trên GitHub và trang tin. Riêng quốc gia của owner repo chỉ lưu mã 2 chữ cái (ví dụ `VN`), suy ra từ trường `location` công khai, không lưu thành phố.

## 2. Các bảng và quan hệ

```
categories ──< repo_categories >── repos ──< repo_snapshots      (số star mỗi ngày)
                                     │ ├──< repo_i18n            (tóm tắt vi/en)
                                     │ ├──< repo_installs        (lệnh cài trích từ README)
                                     │ ├──< repo_flags           (cảnh báo)
                                     │ └──< releases ──< release_i18n
                                     │
source_state ──< news_items ──< news_i18n
                     └──< news_reports                              (báo sai)

doc_pages            (dấu vết các trang docs đang theo dõi)
pipeline_runs        (nhật ký mỗi lần chạy, để kiểm tra "7 đêm liên tiếp không lỗi")
schema_migrations    (các migration đã chạy)
```

`A ──< B` nghĩa là một dòng ở A có nhiều dòng ở B. Xóa một repo thì mọi dữ liệu con của nó (snapshot, tóm tắt, release…) bị xóa theo (`ON DELETE CASCADE`).

| Bảng | Một dòng là | Số dòng ước tính |
|---|---|---|
| `repos` | Một repo GitHub đang theo dõi | 3.000–5.000 |
| `repo_snapshots` | Số star và fork của một repo trong một ngày | khoảng 1,8 triệu/năm |
| `categories` | Một nhóm của Radar (skills, mcp…) | 9 |
| `repo_categories` | Repo thuộc nhóm nào, do đâu mà biết | khoảng 6.000 |
| `repo_i18n` | Tóm tắt của một repo bằng một ngôn ngữ | khoảng 10.000 |
| `repo_installs` | Một lệnh cài của repo | khoảng 5.000 |
| `repo_flags` | Một cảnh báo của repo | khoảng 5.000 |
| `releases` | Một bản phát hành trên GitHub | vài nghìn/năm |
| `release_i18n` | Điểm nổi bật của release bằng một ngôn ngữ | gấp 2 `releases` |
| `source_state` | Tình trạng lần lấy gần nhất của một nguồn tin | khoảng 15 |
| `news_items` | Một tin | vài nghìn/năm |
| `news_i18n` | Tiêu đề và tóm tắt tin bằng một ngôn ngữ | gấp 2 `news_items` |
| `news_reports` | Một lần tin bị báo sai | ít |
| `doc_pages` | Một trang docs đang theo dõi | khoảng 30 |
| `pipeline_runs` | Một lần chạy job | khoảng 3.500/năm |

## 3. Schema đầy đủ (bản nháp của `0001_init.sql`)

```sql
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
```

Dữ liệu khởi tạo (`0002_seed_categories.sql`): 9 category ở PLAN.md mục 6.2.

Bảng `schema_migrations` do script migration tự tạo (mục 5).

### Vì sao thiết kế như vậy

- **Khóa chính của `repos` là id của GitHub**, không phải `owner/name`. Repo đổi tên hoặc đổi chủ thì vẫn là một dòng, lịch sử star không bị mất.
- **Lệnh cài nằm ở bảng riêng (`repo_installs`), không nằm trong tóm tắt.** Lệnh cài không phụ thuộc ngôn ngữ và phải trích nguyên văn từ README, không cho AI viết (PLAN.md 6.6).
- **Mọi bản tóm tắt đều ghi `provider` và `model`.** Đổi qua lại giữa Claude, Gemini hay OpenAI vẫn biết bản nào do model nào tạo, để so sánh hoặc tạo lại khi cần.
- **`CHECK` chặn dữ liệu sai ngay từ database**, ví dụ ngôn ngữ chỉ là `vi` hoặc `en`, mức quan trọng chỉ từ 0 đến 3. Lỗi trong code pipeline sẽ làm job dừng và báo lỗi, thay vì âm thầm ghi dữ liệu hỏng rồi xuất ra app.
- **`excerpt` của tin chỉ dùng nội bộ** để tóm tắt và đối chiếu với lưới an toàn; không bao giờ xuất ra JSON (tôn trọng bản quyền, PLAN.md 11.4).

## 4. Phân quyền

| Role | Ai dùng | Quyền |
|---|---|---|
| `neondb_owner` (role mặc định của Neon) | Chỉ chủ sở hữu, khi chạy migration (workflow chạy tay) | Toàn quyền, kể cả sửa cấu trúc bảng |
| `pipeline_rw` | Các job GitHub Actions hằng ngày | `SELECT`, `INSERT`, `UPDATE`, `DELETE` trên các bảng; **không** được tạo hay xóa bảng |
| `readonly` | Kiểm tra tay, viết báo cáo | Chỉ `SELECT` |

- Chuỗi kết nối của từng role nằm trong GitHub Secrets: `DATABASE_URL` cho `pipeline_rw` và `DATABASE_URL_MIGRATE` cho owner. Không bao giờ nằm trong code hay log. Trên máy chủ sở hữu, cả ba (thêm `DATABASE_URL_READONLY`) nằm trong `.env`.
- **Cách tạo (đã làm 2026-11-20):** migration `0003_roles.sql` tạo `pipeline_rw` và `readonly` ở dạng `NOLOGIN`, không mật khẩu, rồi cấp quyền. Repo công khai vì thế không chứa bí mật nào. Sau đó owner chạy một lần, ngoài git: `ALTER ROLE pipeline_rw LOGIN PASSWORD '<mật khẩu ngẫu nhiên 32 byte>'` (tương tự cho `readonly`). Mật khẩu do máy tạo và ghi thẳng vào `.env`, không hiện ra màn hình.
- **Đổi mật khẩu:** chạy lại `ALTER ROLE … PASSWORD` bằng kết nối owner, rồi cập nhật `.env` và GitHub Secrets.
- **Đã kiểm chứng trên Neon (2026-11-20):** `pipeline_rw` thêm, sửa, xóa dòng được; không tạo, sửa, xóa, `TRUNCATE` bảng được; không ghi được `schema_migrations`. `readonly` chỉ `SELECT`. Test `PostgresTest.test_roles_can_do_only_their_job` kiểm tra lại điều này mỗi lần CI chạy.
- Repo `aigym-code-data` là repo công khai. Vì vậy workflow chạy migration chỉ chạy tay (`workflow_dispatch`) và chỉ đọc được `DATABASE_URL_MIGRATE` qua GitHub Environment có bật bước duyệt.

## 5. Migration (thay đổi cấu trúc database)

- Mỗi thay đổi là một file SQL đánh số trong `pipeline/migrations/`: `0001_init.sql`, `0002_seed_categories.sql`, `0003_…`.
- `pipeline/migrate.py` chạy các file chưa có trong bảng `schema_migrations(version, applied_at, checksum)`, mỗi file trong một transaction (lỗi giữa chừng thì không có gì bị ghi).
- **Không sửa file migration đã chạy.** Script so `checksum` và dừng nếu phát hiện file cũ bị sửa. Muốn đổi thì viết file mới.
- **Thử trên nhánh database trước:** Neon cho tạo nhánh (bản sao tức thời, gói miễn phí có tối đa 10 nhánh mỗi project). Chạy migration trên nhánh, chạy pipeline thử, rồi mới chạy trên nhánh chính.
- Migration có xóa dữ liệu (`DROP`, `DELETE`, đổi kiểu cột) **cần chủ sở hữu duyệt riêng** trước khi chạy trên nhánh chính.

## 6. Backup và khôi phục

| Lớp | Cách làm | Ghi chú |
|---|---|---|
| 1. Khôi phục theo thời điểm của Neon | Có sẵn | ✅ Xác minh 2026-11-20: project `aigym-code` (gói miễn phí) giữ **6 giờ** (`history_retention_seconds` = 21600). Chỉ đủ để sửa lỗi vừa xảy ra; lớp 2 là lớp bảo vệ chính |
| 2. `pg_dump` hằng tuần | Job trong `weekly.yml` → **mã hóa bằng mật khẩu** (lưu trong GitHub Secrets) → lưu thành artifact của GitHub Actions, giữ 90 ngày | Bắt buộc mã hóa vì repo công khai. Dữ liệu chỉ là thông tin công khai, nhưng không có lý do để ai cũng tải được |
| 3. Dữ liệu dựng lại được | Mất database vẫn chạy lại được discover và refresh. Thứ không lấy lại được là **lịch sử star mỗi ngày** và các bản tóm tắt (tốn tiền tạo lại) | Đây là lý do cần lớp 2 |

**Thử khôi phục:** một lần trong P1 (khôi phục bản dump vào một nhánh Neon, đếm số dòng từng bảng), một lần trước khi gửi duyệt, sau đó mỗi quý.

**Cách làm (2026-11-21):** `scripts/backup.sh OUT` dump dạng custom, mã hóa AES-256 (khóa từ `BACKUP_PASSPHRASE` qua PBKDF2, 600.000 vòng), rồi giải mã và liệt kê lại để chắc bản backup đọc được. `scripts/restore.sh FILE URL` chỉ khôi phục vào database trống (từ chối nếu đã có bảng). Workflow `weekly.yml` chạy Chủ nhật 03:47 UTC, artifact `db-backup` giữ 90 ngày. **Mất `BACKUP_PASSPHRASE` là mất mọi bản backup:** chủ sở hữu giữ một bản trong trình quản lý mật khẩu.

**Lần thử khôi phục đầu tiên (2026-11-21, chạy trên máy):** backup thật 2,5 MB, 29 giây; khôi phục vào database trống trên nhánh Neon tạm `restore-test-2026-11-21`; số dòng 16/16 bảng khớp bản đang chạy (repos 11.101, repo_snapshots 22.129); sai mật khẩu thì giải mã báo lỗi; khôi phục lần hai vào cùng database bị từ chối. Nhánh tạm đã xóa.

## 7. Dung lượng và giới hạn gói miễn phí

Gói miễn phí của Neon (theo FAQ chính thức, tháng 10/2026): **1GB mỗi project**, **100 CU-giờ tính toán mỗi tháng**, 10 nhánh.

- **Dung lượng:** `repo_snapshots` chiếm nhiều nhất, khoảng 150MB mỗi năm kể cả chỉ mục, với 5.000 repo. Các bảng khác dưới 50MB. Đủ dùng khoảng 4–5 năm.
- **Quy tắc giữ dữ liệu:** snapshot cũ hơn 400 ngày chỉ giữ một dòng mỗi tuần (ngày Chủ nhật). Việc gom này chạy trong `weekly.yml`, giúp dung lượng gần như không tăng sau năm đầu.
- **Thời gian tính toán:** Neon tự tắt database khi không có truy vấn.
  - Job hằng đêm chạy khoảng 25 phút.
  - Job tin chính thức chạy 8 lần mỗi ngày, mỗi lần khoảng 8 phút kể cả thời gian chờ Neon tự tắt.
  - Cộng lại khoảng 1,5 giờ mỗi ngày, tức khoảng 11 CU-giờ mỗi tháng ở mức 0,25 CU, dưới xa giới hạn 100.
  - Đây là lý do trạng thái dịch vụ **không** đi qua database: kiểm tra 15 phút một lần thì database gần như không bao giờ tắt.

## 8. Điều kiện để duyệt giai đoạn 3 (✅ chủ sở hữu đồng ý 2026-11-19)

- [x] Chủ sở hữu đồng ý danh sách bảng và quan hệ (mục 2).
- [x] Đồng ý phân quyền 3 role (mục 4) và quy tắc migration (mục 5).
- [x] Đồng ý cách backup, đặc biệt là mã hóa bản dump (mục 6).
- [x] Đồng ý quy tắc giữ dữ liệu sau 400 ngày (mục 7).
