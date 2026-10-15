# XPD Source — Git 관리용 프로젝트

Python 3.9 이상, 외부 패키지 없이 실행합니다.
이 폴더 내용 전체를 Git 저장소에 올리면 됩니다. 현재 편집 내용까지 복사했습니다.

## 구성

- xpd.py: XPD 추출·생성·동기화 변환기.
- sample-source/scripts/: 스크립트 이름별 TXT 본문.
- sample-source/screens/: 화면 제목별 JSON. 의미가 확인된 표시 문구 편집용.
- sample-source/texts/: 기타 문자열 보존. 기본 수정 금지.
- sample-source/preserved/: 미해석 데이터 보존. 수정 금지, 빌드 필수.
- sample-source/project.json: 조립 정보와 체크섬. 수정 금지.
- SCRIPTS.md, SCREENS.md, SOURCE_README.md: sample-source 안의 대응표 및 설명.
- .gitignore, .gitattributes: 빌드 제외 및 줄바꿈 보존. 숨김 파일도 포함하세요.

## 실행

PowerShell에서 이 README가 있는 폴더로 이동한 뒤 실행합니다.

```powershell
python ./xpd.py check ./sample-source
python ./xpd.py build ./sample-source ./sample-source/build/edited-v1.xpd
```

기존 출력은 덮어쓰지 않으므로 다음 빌드에는 새 파일명을 사용하세요.
XP-Builder에서 XPD를 수정·저장한 경우에는 같은 소스 폴더로 반영합니다.

```powershell
python ./xpd.py sync ./sample-source/build/edited-v1.xpd ./sample-source
```

sync는 자동 병합이 아닌 XPD 기준 교체입니다. 이전 소스는 build/sync-backups에 백업합니다.
TXT만 수정했다면 build만 실행하세요. 오래된 XPD로 sync하면 그 내용으로 돌아갑니다.
기존 스크립트 본문과 화면 문구 편집까지 지원하며 좌표·색상·통신 설정·객체 추가/삭제는 미지원입니다.
생성한 XPD는 XP-Builder에서 열기·데이터 검사 및 변경 동작 확인 후 사용하세요.
원본 XPD 없이 빌드할 수 있지만 sample-source 전체가 필요합니다.

## Git

이 폴더를 새 저장소로 사용할 경우:

```powershell
git init
git add .
git commit -m "Add HMI sources and XPD converter"
```

기존 저장소를 사용한다면 내용물을 해당 저장소에 복사하고 추가·커밋하세요.
원격 연결 및 push는 별도 작업입니다. 자동 업로드하지 않았습니다.
build 결과·백업, 실험 자료, 캐시, 기존 .git 이력은 복사하지 않았습니다.
확정 배포 XPD를 함께 관리하려면 releases/ 같은 별도 폴더를 사용하세요.
