# T07 별도 배포 실행 안내

현재 외부 배포·push 미실행. 기존 T06 서비스·DB·origin의 push 차단을 유지한다. 아래 명령의 `T07_REPO_URL`, `T07_SHA`, `T07_USER`는 실제 확정값으로 바꿔야 한다. 서비스와 저장소가 미정이면 실행하지 않는다. 기존 T06 웹 앱을 선택하거나 재설정하지 않는다.

사용자 확정: T07 배포 계정·도메인·GitHub 저장소 모두 미정. 새 계정이 필수는 아니며 기존 계정에서 별도 웹 앱을 유지할 수 있다면 사용할 수 있다. 기존 계정이 한 앱만 지원한다면 T06을 교체하지 말고 별도 배포 공간을 선택해야 한다. 요금제 가입은 진행하지 않았다. 새 GitHub 계정은 필요 없고 기존 계정에 T07 저장소를 추가할 수 있다. 기술적으로 같은 저장소의 별도 브랜치도 가능하지만 현재 T06 원격 쓰기 금지와 과제 분리를 위해 새 T07 저장소를 권장한다.

## 1. 전용 저장소 게시 (사용자 작업)

새 빈 T07 공개 GitHub 저장소를 만든다. 비공개 저장소라면 공개 설명 자료를 별도로 게시해야 한다. PowerShell에서:

```powershell
cd C:\work\aleph\t07-plan-do-see-auth
git status --short
git rev-parse HEAD
git remote -v
git remote add t07 T07_REPO_URL
git push -u t07 t07-auth
```

`t07`이 이미 있으면 `git remote get-url t07`로 확인하고 임의 변경하지 않는다. origin push 차단은 해제하지 않는다. 게시 후 시크릿 창에서 `https://github.com/소유자/T07저장소/blob/전체SHA/docs/t07-submission.md`와 `https://github.com/소유자/T07저장소/commit/전체SHA`를 확인한다.

## 2. T06과 분리된 PythonAnywhere 웹 앱 준비

T07 전용 계정 또는 별도 웹 앱을 만들 수 있는 계정을 사용한다. 기존 T06 하나를 교체하는 방식은 사용하지 않는다. 추가 웹 앱 가능 여부와 Python 버전은 해당 계정에서 확인한다. 다음 Bash 명령은 T07 전용 콘솔에서 실행한다.

```bash
# 실제 새 저장소 URL/로컬 커밋 전체 SHA로 교체
cd ~
git clone --branch t07-auth T07_REPO_URL t07-plan-do-see-auth
cd ~/t07-plan-do-see-auth
git checkout --detach T07_SHA
python3.13 -m venv ~/.virtualenvs/t07-auth
~/.virtualenvs/t07-auth/bin/python -m pip install -r requirements.txt
~/.virtualenvs/t07-auth/bin/python -m pip check
mkdir -p ~/t07-private
chmod 700 ~/t07-private
```

Python 3.13이 제공되지 않으면 제공되는 버전으로 콘솔과 Web 설정을 동일하게 맞춘다. 서버 의존성 설치 결과는 아직 검증하지 않았다. 신규 배포는 빈 T07 DB로 시작한다. 기존 5일 자료는 공개 문서로 설명하며 실제 계정/DB 이전은 이 절차에 포함하지 않는다. `.venv`, `instance`, 백업, JSON을 업로드하지 않는다.

## 3. Web 설정과 WSGI

Web → Add a new web app → Manual configuration에서 새 T07 도메인과 위 가상환경의 Python 버전을 선택한다. Virtualenv는 `/home/T07_USER/.virtualenvs/t07-auth`, 소스는 `/home/T07_USER/t07-plan-do-see-auth`로 지정한다. 해당 **새 웹 앱**의 Web → Code → WSGI configuration file 링크에서 아래 내용을 설정한다. 실제 WSGI 경로는 Web 화면이 제공하는 것을 사용한다.

```python
import os
import sys
os.umask(0o077)
sys.path.insert(0, '/home/T07_USER/t07-plan-do-see-auth')
os.environ['PDS_DB_PATH'] = '/home/T07_USER/t07-private/pds.sqlite3'
os.environ['PDS_COOKIE_SECURE'] = '1'
from app import create_app
application = create_app()
```

`app.run()`이나 개발 서버를 운영 WSGI에서 실행하지 않는다. DB는 소스 밖 영속 홈 경로에 보관하고 정적 파일 매핑에 포함하지 않는다. 정적 매핑은 필요 시 `/static/` → `/home/T07_USER/t07-plan-do-see-auth/static`만 지정한다. 홈·소스 루트·instance·t07-private를 공개 매핑하지 않는다. HTTPS 접속 및 Web의 HTTPS 강제 옵션을 확인한 뒤 Reload한다. 로그인은 `/login`, 가입은 `/signup`이며 자료 화면은 비로그인 시 로그인으로 이동한다.

현재 인증은 서버 DB 세션을 사용하므로 Flask SECRET_KEY가 필요하지 않다. 불필요한 고정 키를 추가하지 않는다. 세션 난수는 서버에서 발급한다. 향후 필요한 운영 비밀값은 공개 저장소가 아닌 운영 환경변수/비밀 저장소로 공급하고 실제 값을 문서·명령 이력에 넣지 않는다. 개발용 `PDS_COOKIE_SECURE=0`을 운영에 적용하지 않는다.

## 4. 최소 공개 검증과 제출

- 로그아웃/시크릿 창에서 HTTPS 앱 URL이 로그인 화면으로 연결되고 공개 GitHub 설명 문서는 로그인 없이 열린다.
- 임시 A/B 계정으로 저장·상호 접근 거절·전체 내보내기 분리를 확인한다. HTTPS 쿠키의 Secure/HttpOnly/SameSite와 로그아웃 뒤 접근 거절을 확인한다. 민감한 쿠키 값을 캡처/게시하지 않는다.
- 임시 계정의 비밀번호 변경·탈퇴와 다른 계정 자료 보존을 확인한다. 앱 Reload 후 임시 저장 자료가 유지되는지 확인한다.
- 백업은 운영자가 SQLite backup으로 일관되게 만들고 공개 경로 밖에 보관한다. 보관 기간·수동 폐기 책임자를 정한다. 백업 복원 시 탈퇴 계정·자료·세션이 살아나지 않도록 별도 검토한다. 현재 자동화된 삭제/복원 처리는 없다.
- 확인한 앱 URL, 공개 설명의 고정 SHA URL, 전체 커밋 URL을 제출한다. 확인하지 않은 URL/배포 성공은 기록하지 않는다.

로그인 시도 제한·계정 복구는 미구현이며 공개 운영 위험으로 남는다. 실제 사용자 비밀정보를 평가용 서비스에 추가하지 않는다.

공식 설정 근거: [PythonAnywhere Flask 설정](https://help.pythonanywhere.com/pages/Flask), [가상환경](https://help.pythonanywhere.com/pages/VirtualenvsExplained). 기존 README의 T06 주소는 과거 운영 이력이다.
