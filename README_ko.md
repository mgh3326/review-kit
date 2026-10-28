# review-kit: 검토 자료 준비·패키지·공유

`prep_video.py`는 원본을 읽어서 검토용 H.264/AAC MP4 조각을 새 폴더에 만듭니다. Python 표준 라이브러리와 이미 설치된 `ffmpeg`, `ffprobe`만 사용합니다. 설치·외부 업로드·원본 덮어쓰기는 하지 않습니다. 기존 `prep_video.py`를 교체하지 말고 이 파일을 별도 위치에서 실행하세요.

```bash
# 선택: 패키지로 설치하면 prep-video 명령이 PATH에 생깁니다.
python3 -m pip install .
prep-video --help
review-kit --help
```

## 검토 자료 흐름

전체 흐름은 **준비 → 패키지 구성 → 공유 → 검수자가 INDEX.md 확인 → done으로 삭제**입니다. 기존 `prep_video.py`와 `prep-video` 명령, 출력 형식은 그대로 사용할 수 있습니다.

### 패키지 구성

영상은 기존 분할 도구로 바로 준비하거나, `review-kit package`로 영상 조각·스틸 이미지·검수 요청 안내를 한 폴더에 모을 수 있습니다.

```bash
review-kit package ./review-package \
  --video ./recording.mp4 \
  --still ./screenshots/screen-01.png \
  --title "변경 사항 검수" \
  --context "검토할 동작과 변경 내용을 적습니다." \
  --check "화면에 보이는 상태와 문구를 확인해 주세요." \
  --check "녹화된 흐름에서 회귀가 있는지 확인해 주세요."
```

패키지에는 `README_review_request.md`, `videos/`의 검증된 분할 결과, `stills/`의 이미지가 들어갑니다. 영상 조각은 기본적으로 파일마다 **십진 45 MB 미만**으로 만듭니다. `--max-mb`로 변경할 수 있습니다. 동영상 패키징에는 FFmpeg/ffprobe가 필요하고, 출력 폴더는 미리 존재하면 안 됩니다. 영상 변환 manifest에는 원본 경로와 ffprobe 메타데이터가 포함되므로 공유 전에 패키지 내용을 확인하세요.

### 프로필과 저장소 설정

프로필은 `~/.config/review-kit/<프로필>.env`의 `KEY=VALUE` 형식 파일입니다. `review-kit --profile <프로필> ...` 또는 `REVIEW_KIT_PROFILE`로 선택합니다. 환경 변수로 값과 자격 증명을 지정할 수도 있습니다. `SUPABASE_ENV_FILE`과 `S3_ENV_FILE`은 기존 별도 env 파일을 읽으며 셸 명령을 실행하지 않습니다. 우선순위는 가져온 파일 < 프로필 < 프로세스 환경 변수입니다.

Supabase 프로필 예시(자리표시자):

```ini
BACKEND=supabase
SUPABASE_BUCKET=<PRIVATE_BUCKET>
SUPABASE_ENV_FILE=/path/to/existing/supabase-credentials.env
```

참조하는 파일에 `SUPABASE_URL`과 `SUPABASE_SECRET_KEY`(또는 `SUPABASE_SERVICE_KEY`)를 둡니다. 설정·자격 증명 파일은 저장소 외부에 보관하고 접근 권한을 제한하세요.

일반 S3 호환 프로필:

```ini
BACKEND=s3
S3_ENDPOINT=<HTTPS_ENDPOINT>
S3_REGION=<PROVIDER_REGION>
S3_BUCKET=<PRIVATE_BUCKET>
S3_ACCESS_KEY_ID=<ACCESS_KEY_ID>
S3_SECRET_ACCESS_KEY=<SECRET_ACCESS_KEY>
S3_ADDRESSING_STYLE=path
```

`S3_ENDPOINT`는 서비스 엔드포인트, `S3_REGION`은 SigV4 리전입니다. 주소 방식은 `path` 또는 `virtual`이며 기본값은 `path`입니다.

기존 시크릿 파일을 쓰는 Cloudflare R2 예시:

```ini
BACKEND=s3
S3_ENV_FILE=/path/to/existing/r2-credentials.env
```

참조 파일에는 일반 `S3_*` 이름 또는 Cloudflare의 `R2_*` 이름(`R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET`, `R2_ENDPOINT`)을 둘 수 있습니다. `R2_*` 이름은 대응하는 `S3_*` 설정으로 변환되며, `R2_ENDPOINT`가 없거나 `https://<account_id>.r2.cloudflarestorage.com` 같은 템플릿이면 `R2_ACCOUNT_ID`로 엔드포인트를 만듭니다. R2를 가져올 때는 `S3_REGION=auto`와 path 주소 방식이 기본값입니다.

| 제공자 | 엔드포인트·리전 설정 | 주소 방식 |
|---|---|---|
| Cloudflare R2 | `S3_ENDPOINT=<R2_ACCOUNT_ENDPOINT_URL>`, 리전 `auto` | `path` 지원 |
| Oracle Cloud Infrastructure Object Storage | `S3_ENDPOINT=<OCI_S3_COMPATIBILITY_ENDPOINT>`; `S3_REGION`은 엔드포인트에 포함된 OCI 리전 식별자와 동일 | 네임스페이스 엔드포인트는 `path`; OCI의 virtual-host 호환 엔드포인트도 선택 가능 |
| NAVER Cloud Platform Object Storage | `S3_ENDPOINT=<NCP_REGIONAL_OBJECT_STORAGE_ENDPOINT>`; `S3_REGION`은 선택한 지역 코드. 현재 문서의 region name: `kr-standard`, `us-standard`, `sg-standard`, `jp-standard`, `de-standard` | 우선 `path` 사용 |

S3 클라이언트는 SigV4 요청과 서명된 GET 링크를 지원합니다. R2는 이 저장소에서 실제 왕복 확인을 합니다. OCI와 NCP는 설정을 문서화했으며 실제 연결 검증은 하지 않았습니다.

공식 엔드포인트 문서: [Cloudflare R2 S3 호환성](https://developers.cloudflare.com/r2/api/s3/api/), [OCI S3 호환성](https://docs.oracle.com/en-us/iaas/Content/Object/Tasks/s3compatibleapi.htm), [NAVER Cloud Object Storage 리전별 엔드포인트](https://api.ncloud-docs.com/docs/en/storage-objectstorage).

### Drive 우선 공유, 보관, 재서명

호출 측에서 Drive 검색을 수행하고 상대 경로와 기존 Drive URL의 매핑 JSON을 전달할 수 있습니다. `review-kit`은 Drive에 연결하지 않습니다. 매핑에 있는 파일은 업로드하지 않고 Drive URL로 연결하며, 없는 파일만 선택한 저장소로 업로드합니다. 폴더의 모든 파일이 매핑되어 있으면 `INDEX.md`만 업로드합니다. 폴더 단위 링크는 INDEX 맨 위에 선택적으로 넣을 수 있습니다.

```bash
review-kit --profile work share upload ./review-package pa-review-123 \
  --days 7 \
  --drive-manifest ./drive-manifest.json \
  --drive-folder-url "<DRIVE_FOLDER_URL>"
```

매니페스트 형식:

```json
{
  "README_review_request.md": "<DRIVE_FILE_URL>",
  "stills/screen-01.png": "<DRIVE_FILE_URL>"
}
```

`INDEX.md`에는 상대 파일 경로, 바이트 크기, 로컬 SHA256, 출처(`drive` 또는 `storage`), 링크와 만료 날짜가 들어갑니다. 명령은 INDEX의 서명된 링크를 출력합니다. 링크를 가진 사람은 유효 기간 동안 파일을 읽을 수 있으므로 지정된 검수자에게만 전달하세요. 서명 링크 유효 기간은 1~7일이며, 만료되어도 저장 파일은 자동 삭제되지 않습니다.

```bash
review-kit status                       # 저장소 사용량 및 패키지 상태
review-kit ls [prefix]                  # 원격 객체 목록
review-kit prune --dry-run              # 용량 초과 시 삭제 대상 미리보기
review-kit prune                        # high-water 초과 시 오래된 패키지 삭제
review-kit resign pa-review-123         # 파일 재업로드 없이 링크와 INDEX 갱신
review-kit done pa-review-123           # 검수 종료 후 명시적으로 삭제
```

`--backend supabase|s3`는 저장소를 조회하거나 관리하는 명령의 전역 옵션입니다. 명령 앞에 둡니다(`review-kit --backend s3 done pa-review-123`). `share upload`은 prefix 뒤에 두는 기존 위치도 지원합니다. 삭제와 재서명은 실행 전 원장의 저장소 종류를 확인합니다. 용량 정리는 선택한 저장소에 기록된 패키지만 대상으로 하며, backend 필드가 없는 기존 원장 기록은 현재 선택한 저장소로 처리합니다.

보관 기준은 링크 만료가 아니라 저장소 용량입니다. `prune`은 전체 사용량이 high-water 이하일 때는 삭제하지 않습니다. 초과하면 업로드가 가장 오래된 추적 패키지부터 low-water까지 지웁니다. 기본값은 1 GB 버킷 기준 **high 900 MB / low 700 MB**(십진 MB)입니다. 프로필의 `PRUNE_HIGH_WATER_MB`, `PRUNE_LOW_WATER_MB`로 설정하며 `prune --high-water-mb`와 `--low-water-mb`로 명령별 재정의도 가능합니다. `share upload`도 업로드 전에 같은 용량 정리를 실행합니다.

원장은 기본적으로 프로필 옆 `<프로필>.ledger.json`에 원자적으로 기록되며 `LEDGER_PATH`로 경로를 바꿀 수 있습니다. 저장소에 업로드한 정확한 객체 키와 파일 SHA256만 정리 대상으로 기록합니다. `done`과 `prune`은 Drive 파일이나 원장에 없는 버킷 객체를 삭제하지 않습니다. 보관 중인 패키지 링크가 만료되면 `resign`이 원장의 SHA256과 저장 객체 메타데이터를 비교한 뒤 새 GET 링크를 서명하고 INDEX를 갱신합니다. 파일은 다시 업로드하지 않으며 최초 업로드 날짜도 유지해 오래된 순서가 바뀌지 않습니다.

`status`는 800 MB를 넘으면 경고를 표시합니다.

### MANIFEST.json과 verify

`share upload`는 `INDEX.md` 옆에 `MANIFEST.json`도 만들어 업로드하며, `resign`은 새 링크와 만료일로 이를 갱신합니다. INDEX.md 상단에 링크가 있습니다. INDEX.md의 기계 판독용 짝입니다.

```json
{"schema": "review-kit-manifest/1", "package_id": "pa-review-123", "version": "1",
 "issue_id": "<ISSUE_ID 또는 null>", "created_utc": "...", "expires_utc": "...",
 "files": [{"relative_path": "stills/screen-01.png", "bytes": 0, "sha256": "...",
            "source": "storage", "url": "<서명 링크 또는 Drive 링크>"}]}
```

`--version`(기본 `1`)과 `--issue`(기본 `null`)는 한 줄짜리 자유 문자열입니다. 예: `review-kit share upload ./review-package pa-review-123 --issue <ISSUE_ID> --version 2`. `INDEX.md`와 `MANIFEST.json`은 자동 생성되므로 입력 폴더에 같은 이름의 파일이 있으면 안 되고, 두 파일은 목록에 들어가지 않습니다.

**매니페스트는 INDEX.md와 같은 비공개 자료입니다.** 같은 서명 링크가 들어 있으므로, 가진 사람은 링크가 만료될 때까지 목록의 파일을 읽을 수 있습니다.

검수자는 파일을 폴더에 내려받은 뒤 다음으로 확인합니다.

```bash
review-kit verify ./downloaded-package ./MANIFEST.json
```

목록의 모든 파일의 크기와 SHA256을 비교하고, `MISSING`·`MISMATCH`·`EXTRA` 줄과 개수를 출력하며, 문제가 하나라도 있으면 0이 아닌 코드로 종료합니다. 폴더 안의 `INDEX.md`와 `MANIFEST.json`은 extra로 세지 않습니다. 프로필이나 네트워크가 필요 없습니다.

### deliver: Drive 복사 → Dropbox → 스토리지 대체

`deliver`는 검수자가 Drive → Dropbox → 스토리지 순서로 확인하는 경우의 수작업 절차를 한 명령으로 만든 것입니다.

```bash
review-kit --profile work deliver ./review-package pa-review-123
```

1. 패키지를 `DRIVE_MOUNT_DIR/<이름>/`에 복사합니다. Drive for desktop 마운트의 로컬 경로이거나 `host:/path` 같은 ssh 대상이 될 수 있습니다(각 파일을 `ssh`로 스트리밍하며 추가 도구가 필요 없습니다). 복사본은 `MANIFEST.json`의 바이트 크기와 SHA256으로 검증합니다. 매니페스트가 없으면 공유 manifest 코드로 생성해 복사본 옆에 둡니다.
2. `DROPBOX_REMOTE/<이름>`에 `rclone copy`로 업로드하고 `rclone check`와 `rclone size`로 검증한 뒤 `rclone link`로 공유 링크를 만듭니다.
3. Dropbox 단계가 실패한 경우에만 설정된 Supabase/S3 백엔드(`share upload`와 같은 서명 링크 방식)로 대체 업로드합니다.

명령은 패키지 안에 작은 `DELIVERY.json`을 쓰고, Drive 경로·Dropbox 링크(또는 스토리지 INDEX 링크)·파일 수·바이트 합·매니페스트 SHA256이 담긴 Markdown 스니펫을 출력합니다. 검수 요청에 그대로 붙여 넣으면 됩니다. `DELIVERY.json`은 자동 생성 파일이므로 복사·계수 대상이 아닙니다.

플래그: `--no-dropbox`는 Drive 마운트에만 복사하고, `--force-supabase`는 Dropbox를 건너뛰고 스토리지로 바로 업로드하며, `--dry-run`은 복사·업로드 없이 계획만 출력합니다. 기본 리모트는 없습니다. `deliver`에는 프로필의 `DRIVE_MOUNT_DIR`와 (플래그로 생략하지 않는 한) `DROPBOX_REMOTE`가 필요합니다.

```ini
DRIVE_MOUNT_DIR=/path/to/Drive-mount/reviews
# 또는 ssh 대상: DRIVE_MOUNT_DIR=host:/path/to/drive/reviews
DROPBOX_REMOTE=dropbox:Project/review
# 선택: Drive 업로드가 클라우드에 보일 때 yes/no를 출력하는 명령
DRIVE_CHECK_CMD=/path/to/drive-visible.sh
```

`rclone`과 `ssh`가 `PATH`에 있어야 하며 rclone에는 Dropbox 리모트가 설정되어 있어야 합니다. 전달 이름은 영문·숫자·점·밑줄·대시와 선택적 `/` 중첩을 허용합니다.

Drive 마운트에 복사되면 Drive for desktop의 클라우드 업로드가 백그라운드에서 계속 진행 중이어도 전달된 것으로 간주합니다. 클라우드에 실제로 보이는지는 `drive-visible`이 플러그형 검사로 확인합니다. `DRIVE_CHECK_CMD`에 `yes` 또는 `no`를 출력하는 명령을 지정하세요(환경 변수 `REVIEW_KIT_DRIVE_NAME`과 `REVIEW_KIT_DRIVE_PATH`를 받습니다). 훅이 없으면 `unknown`입니다.

```bash
review-kit --profile work drive-visible pa-review-123   # yes, no 또는 unknown 출력
```

Drive에 보이게 되면 임시 Dropbox 복사본과 스토리지 패키지를 삭제합니다.

```bash
review-kit --profile work cleanup pa-review-123 --dry-run
review-kit --profile work cleanup pa-review-123                     # 먼저 Drive 확인 실행
review-kit --profile work cleanup pa-review-123 --drive-confirmed   # 직접 확인한 경우
```

`cleanup`은 검사 결과가 `no`나 `unknown`이면 `--drive-confirmed` 없이는 실행을 거부합니다. Dropbox 삭제는 보호되어 있습니다. `DROPBOX_REMOTE` 아래의 기록된 리모트 경로만 `rclone purge`로 지우고, 스토리지에서는 원장에 추적된 객체만(`done` 규칙) 삭제합니다. 모든 전달은 `~/.local/state/review-kit/deliveries.json`에 기록되며(`DELIVERIES_PATH`로 이동 가능), `review-kit deliveries`가 아직 정리를 기다리는 전달을 보여주고 `--all`은 전부를 보여줍니다.

## 빠른 시작

```bash
python3 prep_video.py "/path/to/recording.mp4"
```

기본값은 **조각당 최대 120초**, 높이 최대 **1080px**, 영상 **5000 kb/s**, 오디오가 있으면 AAC **96 kb/s**, **30fps**, 파일당 **95,000,000바이트 미만**입니다. 원본 해상도가 작으면 확대하지 않습니다. 실제 시간 길이는 용량 예산 때문에 더 짧아질 수 있습니다. 가로·세로 방향은 유지하며, 홀수 해상도는 짝수로 내림하고 픽셀 종횡비를 정규화합니다.

출력은 원본 옆의 `recording_review_날짜_고유시각/`에 저장됩니다. 직접 지정할 수도 있습니다. **지정 폴더가 이미 있으면 빈 폴더라도 거부**합니다. 중단된 작업의 재개 기능은 없으므로 새 경로를 지정하세요.

```bash
python3 prep_video.py recording.mp4 -o ./review_run_01
```

작은 게임 HUD·자막·인벤토리를 읽을 때는 1080 옵션을 유지하고 필요하면 비트레이트를 높이세요. 720은 작은 글자가 뭉개질 수 있습니다. 원본에 없는 글자 세부 정보는 복원되지 않습니다. 세로 영상의 1080 높이는 폭이 약 608px일 수 있으므로 작은 글자를 먼저 샘플로 확인하세요. 원본이 1920 높이라면 `--height 1920`도 가능합니다.

```bash
python3 prep_video.py recording.mp4 --height 1080 --vkbps 6000 --max-seconds 60
# 기존 스타일의 인자도 그대로 허용됩니다.
python3 prep_video.py recording.mp4 --max-mb 95 --height 720 --vkbps 2500
```

## MB와 MiB

- `--max-mb 95`: 십진 MB, 1 MB = 1,000,000바이트. 기본 제한은 **95,000,000바이트 미만**입니다.
- `--max-mib 95`: 이진 MiB, 1 MiB = 1,048,576바이트. **99,614,720바이트 미만**입니다.
- 두 옵션은 동시에 사용할 수 없습니다. 소수 제한은 바이트로 내림한 후 **엄격한 `<`** 비교를 합니다.
- 기존 스크립트의 `--max-mb 95`가 `95 × 1024²`였다면 이 버전의 같은 명령은 더 보수적인 제한입니다. 기존 바이트 수를 원하면 `--max-mib 95`를 쓰세요.

초기 계획은 제한 바이트의 85%를 영상+오디오 비트레이트 예산으로 나눠 계산하고, 최대 길이와 비교해 더 짧은 쪽을 택합니다. 전체 길이를 같은 수의 구간으로 나누므로 짧은 마지막 조각이 생기지 않습니다. 예산 계산만 믿지 않고 **실제 생성 바이트를 다시 확인**합니다.

## 오디오 출처

기본 `--audio-origin unknown`은 출처 미확인입니다. 이 값은 사람이 제공하는 설명이며 자동 검증 결과가 아닙니다.

```bash
# 실제 녹음임을 사용자가 확인한 경우에만
python3 prep_video.py recording.mp4 --audio-origin actual
# 합성·재구성한 테스트 소리가 들어간 경우
python3 prep_video.py synthetic_test.mp4 --audio-origin reconstructed
```

첫 번째 오디오 스트림을 AAC로 재인코딩합니다. 오디오가 없으면 만들지 않으며 조각의 `audio_origin`을 `absent`로 기록합니다. 원본의 첫 번째 오디오 트랙 이후 추가 트랙은 포함하지 않습니다. 소리가 있는 파일이어도 실제 기기에서 출력된 소리인지, 사용자에게 들렸는지, 게임 이벤트와 일치하는지는 이 도구가 증명하지 않습니다. 이 도구는 소리를 생성하거나 복원하지 않습니다.

## 조각과 원본 시간 대응

성공 폴더에는 다음이 있습니다.

|파일|내용|
|---|---|
|`part_000.mp4`, `part_001.mp4`, …|검증 완료된 조각; 번호는 0부터|
|`manifest.json`|원본 경로·전체 ffprobe 메타데이터·원본 SHA256, 설정·도구 버전·실행 명령·재시도, 조각별 시간·해상도·바이트·SHA256·오디오 출처|
|`segments.csv`|조각별 원본 시작/끝, 로컬 시작 0, 계획 길이·실제 길이, 크기·해시·오디오 출처. UTF-8 BOM 포함|
|`REVIEW_README.md`|사람이 읽는 조각별 원본 시간표|
|`DONE.txt`|모든 단계 성공 후 마지막으로 생성하는 완료 표식|
|선택 JPEG|요청 시각을 파일명과 manifest에 기록한 썸네일|

예: `part_002.mp4`의 원본 시작이 180.5초이고 해당 조각의 로컬 시각이 12.0초라면 원본은 대략 **192.5초**입니다. 검토 요청에는 파일명과 로컬 시각을 함께 적어 주세요. 다른 조각의 같은 로컬 시각과 혼동하지 않게 합니다.

계획 구간은 `[시작, 끝)`으로 연결되며 인접 경계는 같은 수치이고 마지막 끝은 원본 영상 길이와 같습니다. 오디오보다 영상 스트림의 길이를 우선 사용합니다. 영상 스트림 길이를 알 수 없으면 컨테이너 길이를 사용하며 `duration_basis`에 기록합니다. 시간은 FFmpeg 재생 시작 기준의 상대 초이며 컨테이너의 절대 PTS나 촬영 시각이 아닙니다.

변환은 입력 탐색 후 정확한 재인코딩을 사용하지만, **30fps 변환과 경계 프레임 반올림 때문에 프레임 단위 무손실 절단은 아닙니다**. 계획 구간에 틈은 없어도 경계의 장면이 한 프레임가량 반복·제외되거나 시각이 어긋날 수 있습니다. 영상 길이는 계획 길이 대비 최대 `2/30 + 0.02`초 오차를 허용하고 초과하면 실패합니다. 한 프레임보다 짧은 원본도 최소 한 출력 프레임으로 표현할 수 있습니다. AAC 패딩 때문에 MP4 전체 길이는 계획보다 최대 150ms 길 수 있습니다. `video_duration_seconds`와 컨테이너의 `actual_duration_seconds`를 모두 남깁니다. 원본 시각은 검토 참조용이며 정밀 편집·법적 타임코드 증명용이 아닙니다.

## 타임스탬프 썸네일

별도 Python 이미지 패키지 없이 FFmpeg로 JPEG를 생성합니다. 기본은 꺼져 있습니다.

```bash
python3 prep_video.py recording.mp4 --thumbnail-every 30
```

각 조각의 0초부터 30초 간격으로 한 장씩 생성합니다. 이름에 `part`, `local`, `original` 초가 포함되고 manifest에도 대응표가 있습니다. 프레임 선택은 요청 시각과 한 프레임가량 다를 수 있습니다. 작은 썸네일은 장면 찾기용이며 HUD 판독은 MP4로 하세요. **MP4의 HUD 위에 시간을 덧씌우지 않습니다.** 조각별 10,000장을 초과하는 요청은 거부합니다. 썸네일은 업로드 파일 크기 제한 대상 MP4와 별도입니다. 썸네일/메타데이터 생성까지 실패 없이 끝나야 DONE이 생깁니다.

## 실패와 안전장치

- 입력 파일·유한 양수 인자·도구 실행·H.264/AAC 인코더·영상 스트림·길이를 확인합니다. `NaN`, 무한대, 0·음수, 지나치게 작은 제한(16,384바이트 미만)을 거부합니다.
- 인코딩은 기본 조각당 최대 **6회**입니다. 실제 크기가 제한 이상이면 영상 비트레이트를 보수적으로 낮춥니다. `--attempts`, `--min-vkbps`로 조절할 수 있으며 기본 하한은 64 kb/s입니다. 하한 미만이나 마지막 시도까지 초과하면 오류로 종료합니다. 오디오·MP4 헤더만으로 예산이 부족해도 무한 반복하지 않습니다.
- PATH에서 `ffmpeg`/`ffprobe`를 찾고(설치 경로·CPU 이름 하드코딩 없음) libx264/AAC 인코더와 `scale`(`in_range`/`out_range`), `setsar`, `fps`, `format`, `setparams` 필터를 먼저 확인합니다. 없으면 출력 전에 실패합니다. 항상 소프트웨어 libx264로 인코딩하므로 Apple Silicon·Intel Mac에서 같은 명령을 씁니다.
- **색 범위**: 출력은 항상 제한 범위(`tv`) yuv420p입니다. 원본의 색 공간·전달 특성·원색 태그는 그대로 유지하며 새로 지어내거나 덮어쓰지 않습니다. 정책은 `manifest.json`의 `color`에 기록됩니다.
  - 전체 범위(`yuvj*` 또는 `color_range=pc`, 예: iPhone 화면 녹화): 스케일러가 실제 픽셀을 0/255→16/235로 변환한 뒤 `tv`로 표시합니다. 수정 전에는 `yuvj420p`/`pc`로 출력되어 검증에 실패했습니다.
  - 제한 범위(`tv`): 이중 변환 없이 유지합니다.
  - 범위 미지정 YUV: H.264/HEVC 기본값이자 FFmpeg 스케일러의 실제 해석인 제한 범위로 처리하고, 안내(NOTE)를 출력하며 manifest에 기록합니다. RGB 입력은 제한 범위 YUV로 변환합니다.
  - 태그 충돌(예: `yuvj420p`인데 `color_range=tv`, 스트림 태그와 디코딩된 첫 프레임 불일치)은 출력 폴더 생성 전에 거부합니다.
  - **HDR**(PQ/SMPTE 2084, HLG/ARIB STD-B67, mastering display·content light·Dolby Vision 부가 데이터)은 톤 매핑을 구현하지 않았으므로 출력 폴더 생성 전에 거부합니다. SDR(BT.709)로 먼저 내보내세요.
- 크기가 맞더라도 ffprobe의 코덱·짝수 해상도·yuv420p·제한 범위·SDR 색 태그 유지·길이 및 MP4의 faststart 배치를 확인하고 **영상+오디오 전체 디코딩**으로 손상을 검사합니다. 크기 예산이 품질을 보증하지는 않습니다. 저비트레이트 재시도 결과는 설정과 시도 기록으로 확인하세요.
- 임시 `.incomplete.mp4`를 검증한 다음 같은 폴더에서 하드링크로 원자적으로 최종 이름을 확정합니다. 최종 이름이 있으면 덮어쓰지 않습니다. 보통 로컬 디스크를 사용하세요. 하드링크를 지원하지 않는 파일시스템에서는 안전하게 오류가 납니다.
- 모든 조각·메타데이터·선택 썸네일 및 원본 변경 여부 검사를 통과해야 `DONE.txt`를 씁니다. 기존 원본 해시를 마지막에 다시 비교합니다. 녹화·복사 중인 파일은 먼저 닫으세요.
- 실패 후 완료된 조각은 보존되지만 작업 전체는 `manifest.json`의 `status: failed`와 `ERROR.txt`로 표시되고 DONE은 생기지 않습니다. 검증 오류에는 실제 `codec_name`/`pix_fmt`/`width`/`height`/`color_range`와 실패 조건이 포함되며, manifest의 `diagnostics`에 실행 인자(`argv`), 마지막 FFmpeg 명령, 도구 경로, 실패 관측값이 남습니다. 강제 종료·정전·Ctrl-C 시 `running` 상태나 임시 파일이 남을 수 있습니다. **DONE이 없는 폴더는 완료본으로 취급하지 마세요.**
- `--timeout 600`은 각 외부 프로세스별 시간 제한입니다. 느린 컴퓨터에서는 더 큰 값을 쓰세요. 무한 라이브 입력은 지원하지 않습니다. 깨진 입력, 탐색 불가 파일, 메타데이터 길이가 잘못된 파일, 표지 그림이 첫 영상 스트림인 파일은 실패할 수 있습니다. 영상 시작 PTS와 컨테이너 시작 PTS가 1/30+0.02초를 초과해 다르면, 시간표 오해를 막기 위해 출력 폴더 생성 전에 명확한 오류로 거부합니다. 오디오만 늦게 시작하거나 영상보다 먼저 끝나는 일반적인 파일은 지원합니다. AAC 프라이밍 때문에 패킷 시작 PTS는 들리는 소리의 시작과 다를 수 있습니다.
- 전체 source metadata에는 경로·태그가 포함될 수 있습니다. 외부에 공유할 때 필요한 항목만 선택하세요. 이 도구 자체는 업로드하지 않습니다.

오류 예시 및 대응:

```bash
# 글자 품질을 유지하고 재시도 횟수를 줄이려면 조각 길이를 줄입니다.
python3 prep_video.py recording.mp4 --max-mb 20 --max-seconds 20 --height 1080 --vkbps 5000
# 작은 용량에서 영상이 흐리면, 낮은 비트레이트로 억지 완료하지 말고 하한을 지정합니다.
python3 prep_video.py recording.mp4 --min-vkbps 2500 --vkbps 5000
```

## 테스트

```bash
PYTHONPYCACHEPREFIX=/tmp/prep_video_pycache python3 test_prep_video.py && python3 test_review_kit.py
```

설치된 FFmpeg로 0.017~1.4초 합성 영상을 생성합니다. 실제 기기 녹음이 아니며 생성 오디오는 테스트에서 `reconstructed`로 표기합니다. 음성/무음·홀수 크기·세로·회전 90·전체/제한/미지정 색 범위(흑/백 픽셀 값 ±3 검사)·수정 전 전체 범위 실패 재현·HDR 거부·색 태그 충돌·필터 부재·초단편·십진/이진 바이트 제한·입력 오류·도구 부재·출력 충돌·손상/시간 초과·검증 실패·재시도 상한/복구를 검사합니다. 재시도 크기 초과·디코딩 실패·시간 초과는 결정적인 검증을 위해 일부 함수를 모의 처리하며, 정상 변환·디코딩·크기/해시 검사는 실제 도구를 사용합니다. 결과는 `test_artifacts/test_results.json`, 로그와 합성 산출물로 남습니다. 원본 실영상 전체를 인코딩하지 않습니다.

`test_review_kit.py`는 네트워크 없이 가짜 저장소를 사용합니다. Drive/스토리지 혼합 INDEX, Drive 전용 패키지, 원장 정리, 용량 기준 오래된 순서의 prune, 파일 재업로드 없는 resign, 안전한 객체 키, AWS 공개 SigV4 서명을 검사합니다.

이 도구가 플랫폼의 영상 업로드·재생·모델의 영상 이해 능력을 보장하지는 않습니다. 짧은 조각, 원본 시간표, 선택 썸네일이 검토 근거를 추적하기 쉽게 만듭니다.

## 기존 CLI 호환성과 기본값 변경

위치 인자 `input`과 `--max-mb`, `--height`, `--vkbps`를 유지합니다. `--height 720 --vkbps 2500`을 명시하면 이전 해상도·비트레이트 선택을 재현합니다. 기본값 1080/5000은 작은 HUD 글자가 축소로 사라지는 것을 줄이고 늘어난 픽셀 수에 맞는 비트레이트를 주기 위해 선택했습니다. 원본이 더 작으면 확대하지 않습니다. 기본 길이는 120초 이하로, 용량만으로 매우 긴 조각을 만들던 동작에서 검토하기 쉬운 길이로 바뀌었습니다.

```bash
python3 prep_video.py "/path/to/input.mp4" \
  --max-mb 95 --height 1080 --vkbps 5000 --max-seconds 120 \
  --audio-origin unknown --thumbnail-every 30 -o "./review_run_01"
```

95 MB는 항상 95,000,000바이트 미만입니다. 기존 이진 단위 크기는 `--max-mib 95`로 선택합니다. 무한 재시도는 기본 최대 6회·64 kb/s 하한으로 바뀌었고, 실패하면 DONE을 만들지 않습니다. 오디오가 없는 입력은 실제 출력과 manifest에 `absent`로 기록됩니다.

## 개발·플랫폼 검증

색 범위 수정 후 로컬 테스트 통과 환경: Apple Silicon Mac(arm64), macOS 27.0.1, Python 3.14.7, FFmpeg/ffprobe 9.0.2(Homebrew). 같은 환경에서 245.7초 전체 범위(`yuvj420p`/`pc`, BT.709, 회전 90) HEVC 화면 녹화를 기존 CLI 옵션 그대로 3개 조각까지 끝까지 처리·검증했습니다. **Intel Mac(x86_64)은 색 범위 수정 후 아직 재검증하지 않았습니다.** 수정 전 코드는 Intel Mac, macOS 26.6.2, Python 3.9.6, FFmpeg/ffprobe 8.1에서 테스트를 통과했습니다. CI의 `macos-latest`는 Apple Silicon이므로 Intel 검증이 아닙니다. CI는 ubuntu-latest·macos-latest·windows-latest × Python 3.9/3.12/3.14 전체 9개 잡이 run [39414034940](https://github.com/mgh3326/review-kit/actions/runs/39414034940)에서 통과했습니다(`.github/workflows/ci.yml`). FFmpeg/ffprobe는 별도 외부 런타임 의존성이며 이 도구가 자동 설치하거나 프로젝트에 포함(번들)하지 않습니다. Python 외부 패키지는 필요하지 않습니다. 정상 출력은 실제 FFmpeg로 검사하고, 크기 초과·시간 초과·일부 검증 실패는 명시적으로 모의 주입합니다. 오디오 지연, 영상보다 먼저 끝나는 오디오, 한국어·공백 경로, 하한과 최종 메타데이터 실패도 검사합니다.
