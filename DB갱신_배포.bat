@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo(
echo ============================================
echo   군포시 자치법규 - DB 갱신 + 공개사이트 배포
echo ============================================
echo(

REM ------------------------------------------------------------
REM  [1/5] OC 키 로드 (oc_key.txt 첫 줄)
REM ------------------------------------------------------------
if not exist "oc_key.txt" (
  echo [오류] oc_key.txt 파일이 없습니다.
  echo        발급받은 OC 키 한 줄만 넣은 oc_key.txt 를 이 폴더에 두세요.
  goto :fail
)
set "LAW_OC_KEY="
set /p LAW_OC_KEY=<oc_key.txt
if "%LAW_OC_KEY%"=="" (
  echo [오류] oc_key.txt 가 비어 있습니다.
  goto :fail
)
if "%LAW_OC_KEY%"=="여기에_OC_키를_붙여넣으세요" (
  echo [오류] oc_key.txt 에 실제 OC 키를 아직 안 넣었습니다.
  echo        메모장으로 열어 발급키로 바꿔주세요.
  goto :fail
)
echo [1/5] OC 키 로드 완료
echo(

REM ------------------------------------------------------------
REM  [2/5] 증분 수집·분석 (라이브 API - 시간 소요)
REM ------------------------------------------------------------
echo [2/5] 증분 수집·분석 중... (수 분 걸릴 수 있습니다)
python -m gunpolaw --batch --deep --incr
if errorlevel 1 (
  echo [오류] 수집·분석 실패. 위 메시지를 확인하세요. ^(로그: batch_incr.log^)
  goto :fail
)
echo(

REM ------------------------------------------------------------
REM  [3/5] 정적 사이트 재생성 (site/ 에 DB 반영)
REM        새 PC 등 site 리포가 없으면 공개 리포를 자동으로 받아온다
REM ------------------------------------------------------------
echo [3/5] 정적 사이트 재생성 중...
if not exist "site\.git" (
  echo   site 리포가 없어 GitHub에서 새로 받습니다...
  if exist "site" rmdir /s /q site
  git clone https://github.com/sura140705-crypto/gunpolaw-view.git site
  if errorlevel 1 (
    echo [오류] site 리포 clone 실패 ^(네트워크/git 설치 확인^).
    goto :fail
  )
)
python -m gunpolaw --export-static site
if errorlevel 1 (
  echo [오류] 사이트 생성 실패.
  goto :fail
)
echo(

REM ------------------------------------------------------------
REM  [4/5] (선택) 코드 리포 변경분 커밋 - 이 폴더가 코드 리포일 때만
REM ------------------------------------------------------------
echo [4/5] 코드 리포 변경 확인 중...
if exist ".git" (
  git add -A
  git diff --cached --quiet
  if errorlevel 1 (
    git commit -m "데이터/코드 갱신 %date%"
    git push
    if errorlevel 1 (
      echo [경고] 코드 리포 push 실패 ^(네트워크/로그인 확인^). 사이트 배포는 계속합니다.
    ) else (
      echo   코드 리포 push 완료
    )
  ) else (
    echo   코드 리포 변경 없음 - 생략.
  )
) else (
  echo   이 폴더는 코드 리포가 아님 - 코드 push 생략.
)
echo(

REM ------------------------------------------------------------
REM  [5/5] 공개 정적 사이트 배포 (GitHub Pages)
REM ------------------------------------------------------------
echo [5/5] 공개 사이트 배포 중...
pushd site
git add -A
git diff --cached --quiet
if errorlevel 1 (
  git commit -m "데이터 갱신 %date%"
  git push
  if errorlevel 1 (
    echo [오류] 사이트 git push 실패. 네트워크/로그인을 확인하세요.
    popd
    goto :fail
  )
  echo   배포 완료  ^=^>  https://sura140705-crypto.github.io/gunpolaw-view/
) else (
  echo   사이트 변경 없음 - 배포 생략.
)
popd
echo(

echo ============================================
echo   갱신 완료! 창을 닫아도 됩니다.
echo ============================================
echo(
pause
exit /b 0

:fail
echo(
echo ============================================
echo   중단되었습니다. 위 오류 메시지를 확인하세요.
echo ============================================
echo(
pause
exit /b 1
