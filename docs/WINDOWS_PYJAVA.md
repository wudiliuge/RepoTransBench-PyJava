# Windows 杩愯鎸囧崡锛歅ython鈫扟ava

杩欎唤鎸囧崡鐢ㄤ簬鍦ㄤ竴鍙版柊鐨?Windows 鐢佃剳涓婅繍琛?RepoTransBench 鐨?Python鈫扟ava 椤圭洰銆?
## 1. 瀹夎鐜

闇€瑕佸畨瑁咃細

- Git for Windows
- WSL 2
- Docker Desktop

鍚姩 Docker Desktop锛屽苟纭浣跨敤 Linux containers銆傛墦寮€ PowerShell 妫€鏌ワ細

```powershell
git --version
wsl --status
docker version
docker info
```

`docker version` 搴斿悓鏃舵樉绀?`Client` 鍜?`Server`銆俙docker info` 涓皯閲?blkio 鎴?cgroup 璀﹀憡閫氬父鍙互蹇界暐锛屽彧瑕佸懡浠よ兘澶熸甯歌繑鍥炪€?
## 2. 涓嬭浇浠ｇ爜

鎶婁笅闈㈠湴鍧€鏇挎崲鎴愬疄闄?GitHub 浠撳簱鍦板潃锛?
```powershell
Set-Location "D:\archtrans"
git clone https://github.com/wudiliuge/RepoTransBench-PyJava.git
Set-Location ".\RepoTransBench-PyJava"
```

浠撳簱鍙互鏀惧湪鍏朵粬浣嶇疆锛屼絾璺緞涓笉瑕佸寘鍚€楀彿銆?
## 3. 棣栨鍒濆鍖?
鍦ㄤ粨搴撴牴鐩綍杩愯锛?
```powershell
powershell -ExecutionPolicy Bypass `
  -File ".\scripts\setup_windows.ps1"
```

鑴氭湰浼氳嚜鍔細

- 鏋勫缓 Python銆丣ava 鍜?Maven 杩愯闀滃儚锛?- 涓嬭浇骞堕獙璇佸畼鏂规暟鎹泦锛?- 鍦?Linux 瀹瑰櫒鍐呰В鍘嬫暟鎹紱
- 鎻愬彇 169 涓彲杩愯鐨?Python鈫扟ava 椤圭洰锛?- 鍒涘缓淇濆瓨鏁版嵁銆佺粨鏋溿€佹棩蹇楀拰 Maven 缂撳瓨鐨?Docker 鍗枫€?
绗竴娆¤繍琛岄渶瑕佷笅杞介暅鍍忓拰鏁版嵁闆嗭紝鏃堕棿鍙兘杈冮暱銆傜綉缁滀腑鏂椂閲嶆柊鎵ц鍚屼竴鍛戒护鍗冲彲锛屾暟鎹泦涓嬭浇鏀寔鏂偣缁紶銆?
鎴愬姛缁撴潫鏃朵細鐪嬪埌锛?
```text
SETUP_VERIFICATION_OK
Setup completed successfully.
```

浠ュ悗閫氬父涓嶉渶瑕侀噸澶嶅垵濮嬪寲銆傝嫢闇€瑕佸己鍒堕噸鏂版瀯寤洪暅鍍忥細

```powershell
.\scripts\setup_windows.ps1 -RebuildImage
```

## 4. 杩愯涓€涓」鐩?
渚嬪杩愯 `OilerNetwork_fossil_cairo0`锛?
```powershell
.\scripts\run_single.ps1 `
  -ProjectName "OilerNetwork_fossil_cairo0" `
  -ModelName "deepseek-v4-flash" `
  -MaxIterations 20
```

绗竴娆¤繍琛屾垨褰撳墠 PowerShell 娌℃湁璁剧疆 Key 鏃讹紝浼氬嚭鐜帮細

```text
Enter DeepSeek API Key:
```

姝ゆ椂鐩存帴杈撳叆鐪熷疄鐨?DeepSeek API Key 骞舵寜 Enter銆傚睆骞曚笉浼氭樉绀?Key锛岃繖鏄甯哥幇璞°€備笉瑕佹妸鐪熷疄 Key 鍐欏叆鑴氭湰銆丷EADME 鎴栦粨搴撲腑鐨?`API_KEY.txt`銆?
鍙傛暟鍚箟锛?
| 鍙傛暟 | 鍚箟 |
|---|---|
| `ProjectName` | 瑕佽繍琛岀殑 Python鈫扟ava 椤圭洰鍚?|
| `ModelName` | 妯″瀷 API 涓娇鐢ㄧ殑妯″瀷鍚?|
| `MaxIterations` | 妯″瀷鏈€澶氭墽琛屽灏戣疆锛屽父鐢ㄥ€间负 20 |

闇€瑕佸揩閫熸鏌ョ幆澧冩椂锛屽彲浠ュ厛杩愯 1 杞細

```powershell
.\scripts\run_single.ps1 `
  -ProjectName "OilerNetwork_fossil_cairo0" `
  -ModelName "deepseek-v4-flash" `
  -MaxIterations 1
```

杩愯缁撴潫浼氭樉绀虹姸鎬佸拰閫€鍑虹爜锛?
```text
Run finished
Status         : ...
Exit code      : ...
Results volume : rtb_pyjava_results_v1
Logs volume    : rtb_pyjava_run_v1
```

甯歌閫€鍑虹爜锛?
- `0`锛氫唬鐞嗕富鍔ㄥ畬鎴愶紱
- `1`锛氫唬鐞嗘姤鍛婂け璐ワ紱
- `2`锛氳揪鍒版渶澶ц凯浠ｆ鏁帮紝涓嶄唬琛?Docker 鍑洪敊锛岀粨鏋滀粛鐒朵細淇濆瓨锛?- `3`锛氳繍琛岃涓柇锛?- `4`锛氱幆澧冩垨杩愯鏃堕敊璇€?
## 5. 瀵煎嚭缁撴灉鍒?Windows

鏁版嵁鍜岀粨鏋滈粯璁や繚瀛樺湪 Docker volumes 涓€傝繍琛屼笅闈㈢殑鍛戒护鍙互鎶婃墍鏈夌敓鎴愰」鐩拰鏃ュ織瀵煎嚭鍒?Windows锛?
```powershell
.\scripts\export_run.ps1 -Mode Results
```

榛樿瀵煎嚭鍒颁粨搴撳悓绾х洰褰曪紝渚嬪锛?
```text
RepoTransBench-exports\results_20260722_120000\
```

鐩綍鍐呭锛?
```text
generated-results\   妯″瀷鐢熸垚鎴栦慨鏀瑰悗鐨?Java 椤圭洰
run-records\         system prompt銆佹瘡杞氦浜掑拰鏈€缁堟憳瑕?export_manifest.txt  鏈瀵煎嚭鐨勫熀鏈俊鎭?```

鐢熸垚鐨?Java 涓氬姟浠ｇ爜閫氬父浣嶄簬锛?
```text
generated-results\妯″瀷鍚峔Python\Java\椤圭洰鍚峔src\main\java\
```

鏈€缁堟憳瑕侀€氬父浣嶄簬锛?
```text
run-records\logs\妯″瀷鍚峔椤圭洰鍚峗Python_to_Java_鏃堕棿鎴砛final_summary.txt
```

## 6. 瀵煎嚭鏁版嵁闆嗗埌 Windows

濡傛灉闇€瑕佹煡鐪嬫垨澶囦唤鍏ㄩ儴 Python鈫扟ava 鏁版嵁锛?
```powershell
.\scripts\export_run.ps1 -Mode Dataset
```

榛樿瀵煎嚭鐩綍绫讳技锛?
```text
RepoTransBench-exports\dataset_20260722_120000\
```

瀹屾暣鏁版嵁淇濆瓨鍦細

```text
python-java-dataset.tar.gz
```

璇ュ帇缂╁寘鍙兘鍖呭惈 Windows 涓嶆敮鎸佺殑鏂囦欢鍚嶏紝鍥犳涓嶈鐩存帴浣跨敤 Windows `tar` 瀹屾暣瑙ｅ帇銆傝剼鏈拰 Docker 鍙互鐩存帴浣跨敤鏁版嵁鍗凤紝涓嶅奖鍝嶆甯稿疄楠屻€?
## 7. 甯歌闂

### 鎵句笉鍒?`docker`

纭 Docker Desktop 宸插畨瑁呭苟鍚姩锛岀劧鍚庡叧闂苟閲嶆柊鎵撳紑 PowerShell锛?
```powershell
docker version
```

### 闀滃儚涓嶅瓨鍦?
閲嶆柊鎵ц鍒濆鍖栵細

```powershell
.\scripts\setup_windows.ps1
```

### 涓嬭浇鎴?Maven 渚濊禆鑾峰彇澶辫触

澶氭暟鎯呭喌鏄复鏃剁綉缁滈棶棰樸€備繚鎸?Docker Desktop 杩愯锛岀劧鍚庨噸鏂版墽琛屽師鍛戒护銆侻aven 渚濊禆浼氱紦瀛樺湪 `rtb_maven_cache_v1` 涓€?
### 鏄剧ず `maximum iterations reached`

杩欒〃绀烘ā鍨嬪凡鐢ㄥ畬璁剧疆鐨勮疆鏁般€傚畠涓嶇瓑鍚屼簬绋嬪簭宕╂簝銆備娇鐢ㄧ粨鏋滃鍑哄懡浠ゆ煡鐪嬬敓鎴愰」鐩拰 `final_summary.txt`銆?
## 8. 鏈€鐭搷浣滄祦绋?
鏂扮數鑴戠涓€娆′娇鐢細

```powershell
# 鍒濆鍖?powershell -ExecutionPolicy Bypass -File ".\scripts\setup_windows.ps1"

# 杩愯
.\scripts\run_single.ps1 `
  -ProjectName "OilerNetwork_fossil_cairo0" `
  -ModelName "deepseek-v4-flash" `
  -MaxIterations 20

# 瀵煎嚭缁撴灉
.\scripts\export_run.ps1 -Mode Results
```


