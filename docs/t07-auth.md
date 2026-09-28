# 과제 7 인증과 계정별 자료 보호

갱신일: 2026-09-18. 작업 폴더는 `C:\work\aleph\t07-plan-do-see-auth`, 브랜치는 `t07-auth`다. 기준 커밋은 `25809d886e4a53c8e0efa8c75100e40d485bae4d`다. 인증 구현 후 사용자가 지정한 과제 6 백업 JSON을 과제 7의 기존 oliver 계정으로 이전한 작업은 완료됐다. 과제 6 폴더와 PythonAnywhere는 수정하지 않았으며 배포·push·커밋은 하지 않았다.

이번 재개 요청에서는 이전 결과와 현재 DB를 읽기 전용으로 확인하고 문서만 정리했다. 재가져오기, 기능·계정·개인 기록·시각·계획 규칙 변경, 테스트 재실행은 하지 않았다. 아래 자동 검사 결과는 **이전 실행 이력**이며, 사용자 브라우저 확인은 ⑤에 별도로 기록했다. 전체 과제 판정은 [체크리스트](t07-checklist.md)를 참고한다.

## ① 무엇으로 붙였나

- 기존 Python 3.14.7 / Flask 3.1.3 / Werkzeug 3.1.8 / SQLite 3.50.4 구조를 유지했다. 새 패키지는 추가하지 않았다.
- 설치된 나머지 패키지: Jinja2 3.1.6, MarkupSafe 3.0.3, click 8.5.0, blinker 1.9.0, itsdangerous 2.2.0, tzdata 2026.4. `requirements.txt`는 그대로다.
- 비밀번호: `werkzeug.security.generate_password_hash(..., method='scrypt')`, `check_password_hash()` 사용. 설치된 버전에서 확인한 매개변수는 `scrypt:32768:8:1`, 무작위 salt 길이는 16자다. 비밀번호 알고리즘은 직접 구현하지 않았다.
- 계정: `users` 테이블. 소문자로 정규화한 아이디에 DB `UNIQUE COLLATE NOCASE` 제약을 적용했다. 아이디는 영문 소문자·숫자·밑줄 3~32자, 비밀번호는 12~128자다.
- 서버 세션: `secrets.token_urlsafe(32)`로 32바이트 난수를 생성한다. 원문은 브라우저 쿠키에만 전달하고, DB `auth_sessions`에는 SHA-256 해시·사용자 ID·발급/만료 시각만 저장한다. 비밀번호 저장에 SHA-256을 쓰는 것은 아니다.
- 로그인 세션은 발급부터 **8시간 절대 만료**, 로그인 전 CSRF용 익명 세션은 **30분**이다. 사용 중 자동 연장은 없다. 새 로그인 때 기존 현재 쿠키의 세션을 폐기하고 새 값을 발급한다.
- 쿠키: `pds_session`, `Path=/`, `HttpOnly`, `SameSite=Lax`. HTTPS 요청 또는 `PDS_COOKIE_SECURE=1` 설정 시 `Secure`. HTTPS 프록시 뒤 운영 환경에는 반드시 `PDS_COOKIE_SECURE=1`을 설정한다.
- CSRF: 세션 난수에서 용도를 구분해 도출한 값을 폼 숨김 필드에 넣고, 변경 요청의 폼 또는 `X-CSRF-Token` 헤더와 일정 시간 비교한다. 유효한 서버 세션도 필요하다. URL의 CSRF 값은 인정하지 않으며 세션 값은 URL에 넣지 않는다.
- Flask의 서명 쿠키 세션이나 JWT를 사용하지 않으므로 이번 구현에 별도 `SECRET_KEY`는 필요하지 않다. 고정 비밀키도 추가하지 않았다. 향후 서명 기능을 도입하면 환경변수 또는 운영 비밀 저장소에서 공급해야 한다.

## ② 왜 골랐나

설치된 Werkzeug의 scrypt는 salt 생성과 검증을 라이브러리에 맡길 수 있고, 추가 패키지 없이 현재 Flask 구성과 맞는다. 공식 문서와 설치된 함수 소스에서 사용법을 확인했다. [Werkzeug 보안 함수 문서](https://werkzeug.palletsprojects.com/en/stable/utils/#werkzeug.security.generate_password_hash)

서버 DB 세션은 만료 시각을 요청마다 검사하고 행을 삭제하여 즉시 폐기할 수 있다. 로그아웃 전 복사한 쿠키와 비밀번호 변경 전 다른 기기의 쿠키도 다음 요청에서 무효화된다. SameSite만으로 CSRF 방어를 대신하지 않고 세션에 묶인 토큰을 함께 검증했다. [OWASP 세션 관리](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html), [OWASP CSRF 방어](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html)

| 검토한 다른 방식 | 이번에 선택하지 않은 이유 |
| --- | --- |
| 평문·복호화 가능한 암호화·단순 SHA 계열 비밀번호 저장 | 비밀번호 검증용 느린 해시와 무작위 salt 요구를 충족하지 않는다. |
| Werkzeug PBKDF2 | 설치되어 있지만 이번 요구가 scrypt이며 별도 대안이 필요하지 않다. |
| Argon2·bcrypt | 별도 패키지와 운영 관리가 필요하다. 이번에는 설치된 Werkzeug scrypt 요구를 따른다. |
| Flask 기본 서명 쿠키만으로 로그인 유지 | 쿠키를 복사한 경우 서버에서 개별 세션을 폐기하려면 별도 상태 관리가 필요하다. |
| JWT | 만료 전 폐기를 위해 차단 목록 등이 필요해 현재 단일 Flask+SQLite 앱에 불필요한 복잡성이 생긴다. |
| Flask-Login·Flask-Session | 도입해도 소유권·CSRF·전체 세션 폐기 정책은 별도로 필요하다. 이번 앱에는 작은 인증 모듈로 범위를 제한했다. |
| SameSite만 사용 | 보조 방어다. 회원가입·로그인부터 명시적인 CSRF 검사를 적용한다. |

## ③ 어디를 어떻게 고쳤나

| 위치 | 변경 내용 |
| --- | --- |
| `auth.py:23` `migrate()` | 기존 스키마 검사, 자료가 있는 구형 DB의 일관된 백업, 계정·세션 테이블, 소유자 열 추가. |
| `auth.py:62` `issue()` | 새 난수 세션, 이전 현재 세션 삭제, 만료 세션 정리. 원문 세션은 DB에 저장하지 않는다. |
| `auth.py:78` `authenticate()` | 로그인/만료 검사, CSRF 검사, URL·쿼리·폼 식별자 및 재시도 요청 ID의 소유권 검사. 공백 제거와 중복 파라미터 검사 포함. |
| `auth.py` `signup/login/logout/change_password` | 한국어 가입·로그인, 같은 로그인 실패 문구, POST 로그아웃, 현재 비밀번호 확인 후 변경 및 전체 세션 폐기. |
| `app.py:61`, `app.py:99`, `app.py:104` | 계획 조회·목록 소유자 제한, 새 계획에 실제 로그인 계정 지정, 개선점 출처 소유권 확인. |
| `tasks.py:71`, `executions.py:63`, `executions.py:122`, `review.py:44` | 계정별 계획 선택지, 실행 기록 이동 대상, 목록·검색·집계·다음 계획 링크 제한. 단일 ID 접근도 공통 인증 훅을 먼저 통과한다. |
| `export_data.py:25` | 9개 자료 테이블 모두 소유 계획을 기준으로 SQL에서 제한. 계정·비밀번호 해시·세션은 내보내지 않는다. |
| `schema.sql`, `contracts/pds-schema-v3.json` | `plans.owner_id` 추가. 관련 자료의 소유권은 계획을 따라간다. DB 계약 v3, 소유자 정보가 포함된 내보내기 형식 v2. 기존 v2 계약 파일은 보존했다. |
| `templates/auth.html`, `templates/base.html`, 기존 POST 폼 | 기존 CSS로 한국어 인증 화면, 계정 표시, 비밀번호 변경·로그아웃, 숨김 CSRF 필드. 과거의 '누구나 볼 수 있음' 안내 제거. 기존 JavaScript의 FormData에도 CSRF 필드가 포함된다. |
| `tests/auth_support.py`, 기존 테스트 6개 파일 | 실제 회원가입·로그인과 CSRF 준비만 추가. 기존 기능 검증은 유지했다. 실제 HTTP 서버 재시작 테스트도 인증 쿠키를 사용한다. 계약 버전 기대값은 새 형식으로 갱신했다. |
| `tests/test_auth.py` | 계정 A·B, 거절 전후 DB 비교, 세션·CSRF·백업·미배정 자료 격리 검사 추가. |

인증 사용자는 URL·요청 본문·헤더의 사용자 ID로 선택하지 않는다. 서버가 쿠키의 해시로 조회한 사용자만 `g.user`로 사용한다. 소유자가 다르거나 NULL이면 404로 거절한다. 거절 전에 자료 쓰기를 하지 않는다. 잘못된 CSRF는 403, 비로그인은 `/login`으로 303 이동한다.

인증 구현을 시작했을 당시에는 과제 7 `instance/` 아래 DB 파일이 없었다. 그 당시에는 테스트 구형 DB로 마이그레이션·백업을 검증했다. 자동 스키마 백업은 구형 DB에 자료가 있을 때 `DB가 있는 폴더/backups/<DB이름>.<고유값>.bak`에 SQLite `Connection.backup()`으로 생성한다. 백업 실패 시 스키마 변경으로 진행하지 않는다. 실제 적용 시 다른 서버/쓰기 작업을 중지한 상태에서 시작해야 한다. 기존 자료의 소유자를 NULL로 남기며, 첫 가입자에게 배정하지 않는다. 이후 사용자 계정·자료가 생긴 뒤 실행한 JSON 가져오기에서는 아래와 같이 실제 백업을 만들었다.

### 과제 6 JSON 이전 — 이전 요청에서 구현·실제 적용 완료

- 구현: `import_data.py` 관리자 CLI. 현재 지원 입력은 과제 6 schema 2 / export format 1이며 웹 경로를 추가하지 않았다. 기존 대상 DB·계정 이름·내부 사용자 ID를 필수로 지정한다. 없는 DB나 계정을 생성하지 않는다.
- `read_export()`·`validate_data()`는 허용한 9개 테이블/필드, 자료형, 날짜·시각, 중복 키, 외래키, 계획/수정 이력, 완료 이벤트/상태 요청의 관계를 검사한다. 독립 메모리 DB로 검사하므로 누락 참조를 대상 DB의 기존 행에 임의로 연결하지 않는다.
- 기본은 읽기 전용 dry-run이다. 적용에는 `--apply`와 dry-run에서 확인한 `--expect-sha256`가 필요하다. `--expect-plan-title`, `--expect-other-user`로 확인용 계획과 두 번째 계정도 검사했다. 같은 파일 재실행은 ID 충돌 오류로 전체 거절하며, 조용히 건너뛰거나 덮어쓰지 않는다.
- `import_export()`는 `BEGIN IMMEDIATE`로 쓰기 예약을 잡고 충돌을 재검사한다. 대상에 쓰기 전 별도 읽기 연결의 SQLite backup으로 일관된 백업을 만든다. 이후 INSERT·원본 비교·건수·관계 검사를 한 트랜잭션으로 수행한다. 실패 시 전체 롤백한다.
- 원본: `instance/pds-export-20260918T004430711319Z.json` 한 개. 대상: `instance/pds.sqlite3`의 기존 oliver. 실제 계정의 비밀번호를 읽거나 바꾸지 않았고 사용자·세션 테이블에 쓰지 않았다.
- 원본 ID·날짜·시각·버전·내용·관계를 보존했으며 소유자만 지정한 oliver로 설정했다. `plan_reviews` 등의 집계 메타데이터로 없는 자료를 만들어내지 않았다.
- 실제 이전 건수: 계획 **2**, 수정 이력 **3**, 할 일 **5**, 태그 연결 **5**, 상태 요청 **4**, 완료 이벤트 **4**, 실행 기록 **3**, 개선점 **1**, 다음 계획 연결 **1**. 기존 확인용 계획·계정·tester2 자료를 유지했다.
- 최초 백업: `instance/backups/pds-before-t06-import-20260918T023223619208Z.sqlite3`. 적용 직전 자동 백업: `instance/backups/pds-before-import-20260918T024013202733Z-6e8bf1e8.sqlite3`.
- 작업 결과는 Git 제외 폴더의 `.tmp/import-applied.json`·`import-verification.json`·`import-rehearsal.json`으로 확인했다. DB에 가져오기 이력 테이블을 추가한 것은 아니다. JSON·DB·백업·임시 결과 파일은 제출물에 포함하지 않는다.
- 이번에는 이미 완료한 자료를 재가져오지 않았다. 원본 해시 불변, 9개 테이블의 원래 필드 일치, oliver 소유 계획 2개 유지, 외래키 위반 0건, 백업 존재를 읽기 전용으로 확인했다. 전후/현재 건수는 [진행 기록](t07-progress.md)에 구분했다.

향후 다른 파일을 가져올 때도 위 CLI의 dry-run과 계정/해시 검증을 먼저 수행한다. 현재 파일은 이미 적용됐으므로 다시 `--apply`하지 않는다. 과제 7 형식 v2 JSON 가져오기, 웹 업로드, 계정 자동 배정은 지원하지 않는다. 복원은 추가된 현재 기록까지 영향을 주는 별도 작업이므로 이번에 하지 않았다.

## ④ 접근이 거절되는 것을 확인한 기록

별도 `instance/tests/<고유값>.sqlite3`에서 자동 검사했다. 아래 `{P}`, `{T}`, `{E}`, `{I}`는 다른 계정의 계획·할 일·실행 기록·개선점 ID를 뜻한다. 세션·CSRF·비밀번호 원문은 표에 기록하지 않았다. 인증 요청의 Cookie/CSRF 값은 모두 **[가림]**이다.

| 실제 요청 경로·방식 | 결과 | 관련 검사 / 구현 |
| --- | --- | --- |
| 비로그인 `GET /`, `/plans/new`, `/plans/unknown`, `/tasks`, `/executions`, `/review`, `/export.json`, `/account/password` 및 각 경로의 POST | 303 `/login` | `test_auth.py:97`, `auth.py:78` |
| A→B와 B→A `GET /plans/{P}`, `GET/POST /plans/{P}/edit` | 모두 404, 수정 이력 포함 자료 불변 | `test_auth.py:179`, `auth.py:78`, `app.py:61` |
| 양방향 `GET /tasks?plan_id={P}`, `GET /tasks/new?plan_id={P}`, `POST /tasks/new` | 모두 404, 추가 없음 | `test_auth.py:179`, `auth.py:78`, `tasks.py:102` |
| 양방향 `GET/POST /tasks/{T}/edit`, `GET/POST /tasks/{T}/delete`, `POST /tasks/{T}/state` | 모두 404, 수정·삭제·완료 기록 불변 | `test_auth.py:179`, `auth.py:78`, `tasks.py:122` |
| 양방향 `GET /executions?plan_id={P}`, `POST /executions`, `GET/POST /executions/{E}/edit` | 모두 404, 자료 불변 | `test_auth.py:179`, `auth.py:78`, `executions.py:63` |
| 양방향 `GET /review?plan_id={P}`, `POST /review` | 모두 404, 개선점 추가 없음 | `test_auth.py:179`, `review.py:44` |
| 양방향 `GET /plans/new?source_improvement={I}`, `POST /plans/new`에 타인 개선점, 타인 다음 계획 `GET /plans/{P}` | 모두 404, 계획·연결 불변 | `test_auth.py:179`, `app.py:104` |
| 자신의 실행 기록 수정 POST에서 타인 task_id로 교체, 자신의 할 일 수정 POST에서 타인 plan_id로 교체, 타인 재시도 request_id 사용 | 모두 404, DB 불변 | `test_auth.py:205`, `auth.py:78` |
| 앞뒤 공백·탭·개행으로 감싼 타인 task_id로 실행 기록 수정, 공백으로 감싼 타인 plan_id로 할 일 추가 | 양방향 404, DB 불변 | `test_auth.py:205`, `auth.py` 식별자 정규화 |
| `POST /plans/new?user_id=타인ID`, 폼 owner_id/user_id와 `X-User-ID`도 위조 | 303 생성되지만 실제 로그인 계정 소유로만 저장 | `test_auth.py:205`, `app.py:104` |
| 계정별 `GET /`, `/tasks`, `/executions`, `/review`, 실행 수정의 선택지 | 200, 타인 자료·ID 없음 | `test_auth.py` `test_lists_search_review_and_all_export_tables_are_isolated` |
| `GET /tasks?q=타인자료의표시어` | 200, 검색 결과 0개. 입력창의 검색어 반영과 결과 자료는 구분 | 위 테스트, `tasks.py:71` |
| `GET /export.json?user_id=위조값`, 사용자 헤더 위조 | 200, 9개 테이블과 집계에 현재 계정 자료만 포함 | 위 테스트, `export_data.py:25` |
| 세션 만료 시각을 지난 상태의 `GET /` | 303 `/login` | `test_auth.py:110`, `auth.py:78` |
| `POST /logout` 후 복사해 둔 쿠키로 `GET /export.json` | 로그아웃 303, 재사용도 303 `/login` | `test_auth.py:134`, `auth.py` `logout()` |
| `POST /account/password` 성공 후 변경 전 두 세션으로 `GET /` | 모두 303 `/login`, B 계정은 200 유지 | `test_auth.py:134`, `auth.py` `change_password()` |
| 변경 뒤 `POST /login`에 이전 비밀번호 / 새 비밀번호 | 각각 400 / 303 | 같은 검사 |
| 자료의 모든 POST 경로·로그아웃·비밀번호 변경에 CSRF 없음/오류/타 세션/비ASCII 값 | 모두 403, DB 전체 불변 | `test_auth.py:158`, `auth.py:78` |
| `POST /signup`, `POST /login`에 CSRF 누락 | 403 | 같은 검사 |
| `POST /logout`에서 URL에만 CSRF 제공 | 403 | 같은 검사 |
| `GET /logout` / `GET /account/password` | 405 / 200(입력 화면), DB 불변 | `test_auth.py:97` |
| 없는 아이디 / 잘못된 비밀번호로 `POST /login` | 둘 다 400, 같은 한국어 안내와 동일 응답 본문 | `test_auth.py:78`, `auth.py` `login()` |
| 같은 비밀번호로 A·B 회원가입 | 두 해시 모두 scrypt이며 저장 해시는 서로 다름. 원문은 DB에 없음 | `test_auth.py:78`, `auth.py` `signup()` |
| 동일 아이디 대소문자 변형 가입 및 직접 DB INSERT | 가입 400 / DB UNIQUE 위반 | 같은 검사 |
| 테스트 구형 DB 마이그레이션 후 첫 가입자 `GET /plans/legacy-plan`, `/export.json` | 상세 404 / 내보내기 200이고 plans 빈 배열. 백업 무결성 `ok`, 원래 자료 보존 | `test_auth.py` `test_legacy_migration_backup_and_unassigned_records_stay_hidden`, `auth.py:23` |

거절 전후 비교는 자료 테이블뿐 아니라 테스트 DB의 모든 테이블을 메모리에서 비교한다. 원문 자격 증명을 증거 파일로 출력하지 않는다. 로그인 때 익명 세션이 바뀌고 이전 세션 해시가 DB에서 삭제되는 것, 서버 만료 간격 28,800초, HttpOnly·SameSite·Secure 속성도 검사했다.

실행 명령:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m pip check
git diff --check
```

인증 구현 당시 전체 테스트: **40개 통과, 실패 0개, 오류 0개, 건너뜀 0개**. 출력은 `Ran 40 tests in 22.567s`, `OK`, 종료 코드 0이었다. 구성은 기존 31개 + 인증 9개다. 당시 pip 의존성 검사와 diff 공백 검사도 통과했다. 실제 HTTP 서버 재시작 검사는 임시 포트와 테스트 DB로 수행했다.

그 후 가져오기 테스트 12개를 추가한 **이전 작업의 전체 실행 결과는 52개 통과, 실패 0개, 오류 0개**다. 출력은 `Ran 52 tests in 25.239s`, `OK`, 종료 코드 0이었다. **이번 상태 점검에서는 테스트를 재실행하지 않았다.**

| 이전 가져오기 검증 범위 | 이전 실행에서 확인한 결과 | 근거 |
| --- | --- | --- |
| 형식·참조·ID/고유키 충돌, 같은 파일 재실행 | 전체 거절, 부분 저장·덮어쓰기 없음 | `tests/test_import.py` 12개 테스트 |
| 마지막 테이블 INSERT 실패 | 앞서 저장하려던 자료까지 전체 롤백 | `test_late_target_failure_rolls_back_all_nine_tables` |
| 기존 계정·자료, 원본 JSON, 백업 | 보존 및 백업 무결성 확인 | `test_import_preserves_every_source_field_accounts_existing_data_and_backup` |
| 실제 JSON의 별도 DB 리허설: `GET /plans/{P}`, `/tasks?plan_id={P}`, `/executions?plan_id={P}`, `/review?plan_id={P}` | 임시 oliver 역할 200 / 임시 tester2 역할 404, 두 계획에 걸쳐 총 16회 | `.tmp/import-rehearsal.json`, `.tmp/rehearse_import.py` |
| 별도 DB `GET /`, `GET /export.json` | tester2 역할에 가져온 자료 미노출, oliver 역할 내보내기는 원본 자료 보존 | 같은 리허설 및 `test_imported_data_is_accessible_only_to_target_through_real_auth_routes` |
| 실제 적용 사후 SQL 비교 | 기존 자료·계정·세션 불변, 원본 필드/관계 보존, 외래키 위반 0건, 무결성 ok | `.tmp/import-verification.json` |

위 인증 HTTP 검사는 별도 테스트 DB와 임시 인증 정보로 수행했다. 실제 계정 비밀번호를 사용한 자동 로그인 검사가 아니다. 이번 읽기 전용 확인은 HTTP 요청 없이 SQL과 파일만 읽었다. 요청의 Cookie/CSRF·비밀번호 값은 문서에 기록하지 않는다.

중간 검사에서는 내보내기 소유자 필드 누락 1건을 발견해 형식과 계약을 수정했다. 이후 새 검색 분리 테스트의 기대값이 입력창에 반영된 검색어까지 자료 유출로 판단해 1건 실패했고, 검색 결과 행 자체를 검사하도록 바로잡았다. 기존 기능 검증을 삭제하지 않았다. 마지막에는 식별자 공백 우회 검사를 추가하고 전체를 다시 실행했다.

AI가 브라우저에서 직접 클릭하거나 실제 HTTPS 배포 환경을 검사하지는 않았다. 위 HTTP 상태는 이전 자동 테스트에서 확인한 값이다. 사용자가 별도로 보고한 브라우저 확인은 아래 ⑤에 기록한다.

## ⑤ AI가 수행한 일과 사용자 판단이 필요한 일

AI는 이전 작업에서 인증·CLI 가져오기 코드 작성, 버전·공식 문서 확인, 별도 테스트 DB 검증, 사용자가 요청한 실제 JSON 이전을 수행했다. 이번에는 읽기 전용 상태 확인과 문서 정리만 수행했다. 세션 8시간·익명 세션 30분·아이디/비밀번호 길이는 구현 기본값이며, 사용자가 정책 전체를 승인했다고 해석하지 않는다.

사용자가 대상 계정을 oliver로 지정한 이전 요청은 완료됐다. 사용자에게 남은 판단은 계정 삭제 시 자료 처리 정책, 공개 운영 전 로그인 시도 제한·복구 수단·HTTPS 정책, 세션/비밀번호 정책의 적합성, 5일 실험의 기록과 변경 전후 비교 기준이다. 배포와 T07 제출용 커밋 준비는 아직 하지 않았다.

### 사용자가 보고한 브라우저 수동 확인

- oliver 회원가입·로그인 후 테스트 계획 저장과 상세 조회 성공.
- tester2로 그 계획 주소 접근 시 ‘계획을 찾을 수 없습니다’ 표시.
- 로그아웃 후 계획 주소 접근 시 로그인 화면 표시.
- 일반 창·시크릿 창에 oliver로 각각 로그인한 뒤, 일반 창에서 비밀번호를 변경하고 시크릿 창을 새로고침하자 로그인 화면 표시.
- 이전 뒤 기존 계획 2개·테스트 계획 1개, 가져온 할 일 5개·실행 기록 3개를 확인.
- 이후 5일 기록용 새 계획을 만들고 1일차 할 일을 생성·완료 처리. 2026-09-18 실행 기록을 저장·수정한 뒤 돌아보기에서 예상 30분·실제 3.05분·차이 -26.95분을 확인.

개인 기록 제목·본문은 생략했다. 이 보고는 사용자 수동 확인이며, HTTP 상태 코드·복사한 쿠키 재사용·A↔B 전체 경로 검증을 사용자가 했다는 뜻이 아니다. 이번 읽기 전용 계산에서도 1일차 수치는 일치했지만, AI가 같은 브라우저 동작을 재현한 것은 아니다.

로컬 실행:

```powershell
cd C:\work\aleph\t07-plan-do-see-auth
$env:PDS_PORT = "5001"
$env:PDS_DB_PATH = "C:\work\aleph\t07-plan-do-see-auth\instance\pds.sqlite3"
$env:PDS_COOKIE_SECURE = "0"
.\.venv\Scripts\python.exe app.py
```

접속: **http://127.0.0.1:5001**. Secure=0은 로컬 HTTP용이다. HTTPS 운영에는 1을 사용한다. 과제 7 전용 DB를 명시하므로 과제 6 DB를 사용하지 않는다. 종료는 `Ctrl+C`다.

추가로 확인할 수 있는 화면 순서(아래 전체를 수행했다는 뜻은 아님):

1. 접속하면 로그인 화면으로 이동하는지 확인한다. 회원가입에서 A 계정을 만들고 로그인한다.
2. 계획→할 일→실행 기록→돌아보기→개선점→다음 계획을 만들어 본다. 각 화면과 전체 자료 내보내기를 확인한다.
3. A의 계획·할 일 수정·실행 기록 수정 주소를 메모한다. 별도 브라우저나 시크릿 창에서 B 계정을 만든다.
4. B에게 A 자료가 목록에 보이지 않고, 메모한 주소를 열면 404가 되는지 확인한다. B의 자료도 만든 후 A에서 반대 방향으로 확인한다. 각 계정의 내보내기도 비교한다.
5. 로그아웃 후 자료 주소를 다시 열면 로그인 화면으로 가는지 확인한다.
6. A로 두 브라우저에 로그인하고 한쪽에서 비밀번호를 변경한다. 양쪽 모두 자료를 다시 요청하면 로그인 화면으로 이동하는지, 새 비밀번호로 로그인되는지 확인한다. B의 로그인은 유지되어야 한다.

이 순서는 안내용이다. 실제 수행한 수동 확인 범위는 위 사용자 보고 목록으로 한정한다.

## ⑥ 아직 못 막은 것과 그 위험

- 로그인/가입 시도 횟수 제한, CAPTCHA, MFA는 없다. 비밀번호 추측·유출 비밀번호 재사용 공격과 scrypt 연산/SQLite 쓰기 잠금에 의한 서비스 방해 위험이 있다. 공개 운영 전 보완해야 한다.
- 이메일 확인·비밀번호 분실 복구·계정 삭제·관리 화면은 없다. 비밀번호를 잊으면 현재 UI로 복구할 수 없다. 가입 시 중복 아이디 안내로 아이디 존재 여부를 추정할 수 있다.
- HttpOnly와 CSRF는 XSS·브라우저 확장·기기 탈취를 모두 막지 못한다. 쿠키가 탈취되면 만료/폐기 전에는 악용될 수 있다. DB 파일과 백업은 암호화하지 않았으므로 운영체제 파일 접근 권한과 백업 보관 정책도 필요하다.
- 로컬 HTTP에서는 Secure를 끈다. 운영 HTTPS·프록시 설정·실제 브라우저 쿠키 정책은 검증하지 않았다. 잘못된 운영 설정은 통신 도청 위험을 만든다.
- 폐기는 이후 요청을 막는다. 폐기 전에 이미 인증을 통과해 처리 중인 요청의 결과를 되돌리는 기능은 없다. 세션 만료는 절대 시간 방식이며 별도 비활동 만료는 없다.
- 마이그레이션은 서버 쓰기를 멈춘 상태를 전제로 한다. SQLite backup API의 일관된 스냅샷은 검사했지만, 여러 운영 프로세스가 동시에 스키마를 바꾸는 상황은 검증하지 않았다.
- 소유권은 공통 요청 검사와 계정별 조회 조건으로 강제한다. 새 엔드포인트/새 식별자를 추가할 때도 검사와 양방향 테스트를 추가해야 한다. DB 관리자가 직접 소유권이나 연결을 바꾸는 작업은 앱 인증의 보호 범위 밖이다.
- AI의 브라우저·모바일·실제 HTTPS 환경 수동 검증, 부하 시험, 외부 보안 감사는 수행하지 않았다. 사용자의 일부 브라우저 확인과 자동 테스트 통과를 이러한 전체 검사의 통과로 해석하지 않는다.
- 계정 삭제와 삭제 시 자료/백업/세션 처리 안내는 미구현이다. 로그아웃은 계정 삭제가 아니다.
- 현재 돌아보기는 전체 기간 합계만 제공한다. 하루별 합계·5일 평균·변경 전 2일/후 3일 평균·소수점 첫째 자리 표시는 미구현이다. 실제 기록도 1일뿐이므로 나머지 날짜와 규칙 변경·비교 결과를 추정하거나 만들어내지 않았다.
- 이번 제출 후보 파일 정적 점검에서는 실제 비밀값으로 판단되는 리터럴을 발견하지 못했고, 탐지된 테스트용 설정 문자열은 실제 비밀키가 아닌 검증 표식이었다. JSON·DB·백업·가상환경·임시 결과는 Git 제외 상태다. 이 점검은 전용 비밀 탐지 도구나 전체 Git 과거 이력 감사가 아니므로 최종 제출 파일/커밋에서도 재확인이 필요하다.


## 계정 삭제 구현 완료 (2026-09-28)

`auth.py:delete_account()`의 `GET /account/delete`에서 삭제 범위·복구 불가·탈퇴 전 내보내기·백업 처리 안내를 보여준다. `templates/base.html`에 계정 삭제 링크, `templates/account_delete.html`에 현재 비밀번호와 필수 동의 입력을 추가했다. POST는 기존 CSRF 검사와 scrypt 비밀번호 확인을 거치며 삭제 대상은 서버의 `g.user`로만 결정한다.

쓰기 잠금 후 비밀번호를 다시 확인하고 연결된 11종 자료(삭제 표시된 할 일 포함), 모든 세션, 계정을 하나의 트랜잭션에서 삭제한다. 오류는 전체 롤백하고 503을 반환한다. 기존 백업·다운로드 JSON은 자동 삭제하지 않는다. 현재 자동 보관 기한/자동 정리 기능은 없으며 운영자가 수동 관리한다. 백업 복원 시 탈퇴 자료와 세션의 재등장 방지도 수동 확인이 필요하고 자동화되지 않았다. 앱의 탈퇴 취소/복구 기능은 없다.

별도 테스트 DB의 `tests/test_account_delete.py` 신규 5개와 기존 인증 9개를 실행해 **서로 다른 14개 통과, 실패 0개**를 확인했다(첫 실행 `Ran 14 tests in 11.749s`, OK). 테스트 모듈의 중복 수집을 막고 다른 계정 전체 내보내기·계정·세션 불변 검증을 보강한 뒤 신규 5개를 최종 재실행해 **5개 통과, 실패 0개**, `Ran 5 tests in 4.016s`, OK를 확인했다. 이전 전체 68개는 재실행하지 않았다. `git diff --check` 통과. 실제 DB/oliver/tester2/5일 기록은 열거나 수정하지 않았고 스키마 변경·배포·push도 하지 않았다. 브라우저 수동 확인은 아직이다.

| 검사 요청 | 실제 자동 검사 결과 | 근거 |
| --- | --- | --- |
| 로그인 상태 GET /account/delete | 200, 안내·내보내기·동의 폼, DB 불변 | test_get_is_read_only_and_explains_backups_and_export |
| POST /account/delete, 올바른 현재 비밀번호·동의·CSRF | 303 /login, 연결 자료·계정·모든 세션 삭제, 쿠키 제거 | test_success_all_tables_other_user_and_all_old_sessions |
| 같은 POST에 다른 user_id/owner_id/X-User-ID | 실제 로그인 계정만 삭제, 다른 계정의 전체 자료·계정·세션 불변 | 같은 테스트 |
| 잘못된 비밀번호 또는 동의 누락/거부 POST | 400, DB 불변, 비밀번호 응답 노출 없음 | test_wrong_password_and_missing_consent_no_changes |
| CSRF 누락/오류/다른 세션 토큰 POST | 403, DB 불변 | test_csrf_rejection_and_anonymous_access |
| 비로그인 GET/POST /account/delete | 303 /login | 같은 테스트 |
| 탈퇴 전 두 세션 쿠키로 GET /export.json | 각각 303 /login | test_success_all_tables_other_user_and_all_old_sessions |
| 마지막 users 삭제에서 오류 주입 후 POST | 503, 모든 자료·세션 롤백, 두 계정 내보내기 200 | test_failure_at_last_delete_rolls_back_every_table_and_sessions |

요청 값의 비밀번호·세션·CSRF 원문은 기록하지 않는다. 소스 위치는 `auth.py:delete_account`, 공통 보호는 `auth.py:authenticate`, 검사는 `tests/test_account_delete.py`의 위 함수들이다. 백업 보관 기한 결정·수동 정리와 복원 운영 절차는 운영자의 후속 판단/처리 사항이다.



### 사용자 브라우저 탈퇴 확인 추가 (2026-09-28)

사용자는 임시 계정으로 브라우저 탈퇴를 확인했고, oliver의 기존 자료와 5일 기록도 그대로임을 확인했다고 보고했다. HTTP 상태·쿠키 재사용까지 수동 확인했다고 확대하지 않는다. 이전 자동 테스트 결과와 구분하며 이번 테스트 재실행은 없었다. 실제 5일 산술 검산 및 9/23·9/28 중복 구간의 한계는 [진행 기록](t07-progress.md)의 최신 절을 따른다. 이번에는 코드·DB 변경, 배포·push 없이 문서만 갱신했다.
