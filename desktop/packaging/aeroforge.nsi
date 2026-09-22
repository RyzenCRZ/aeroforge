; ============================================================================
; AeroForge NSIS 安装包（规格 §16 M7「安装包与分发」）
; ============================================================================
;
; 打包对象（数据包与代码分离，§15 / §16 M7）：
;   - 代码包：PyInstaller onedir 产物 dist-desktop\AeroForge\（整目录）→ $INSTDIR
;   - 数据包：仓库 data\（CEA 预计算表 + GCAT 快照 + GCAT 目录库）→ %LOCALAPPDATA%\AeroForge\data
;     落点依据 backend/aeroforge/paths.py 冻结态语义：data_root() 缺省 = %LOCALAPPDATA%\AeroForge\data，
;     可被环境变量 AEROFORGE_DATA_DIR 覆盖。数据**不进** $INSTDIR：安装到 Program Files 后不可写，
;     而母线存档 / 阈值配置 / artifacts 缓存都是运行期要写入的。
;
; 构建（推荐在仓库根执行；NSIS 相对路径以本 .nsi 所在目录为基准）：
;     .tools\nsis\makensis.exe desktop\packaging\aeroforge.nsi
;   可用 /DVERSION=0.7.1 /DCODE_DIR=… /DDATA_DIR=… 覆盖。产物输出 desktop\packaging\dist\（gitignore）。
;
; 设计要点（对应 M7 验收「安装 → 启动 → 卸载全流程无残留」）：
;   1. WebView2 运行时检测与引导（R-33）：三处注册表视图与 desktop/launcher.py 同口径；
;      缺失时引导到官方 Evergreen 下载页——**不强制捆绑运行时**（微软专有组件免费可再分发，
;      但捆绑会显著增大包体且违背「引导即可」的 M7 口径）。
;   2. ASCII 路径门禁（R-34 / 坑位 4）：目录页离开时校验 $INSTDIR 非 ASCII 即拒绝——
;      与 launcher 的 ascii_root()（退出码 5）同一约束，只是提前到安装期拦下。
;   3. 卸载保留用户数据：artifacts 内容寻址缓存 / 母线存档 / 阈值配置是**用户数据**，
;      卸载默认保留并弹窗确认；确认删除时才整目录清除。
;   4. ⚠ 已知局限（随包报告）：以管理员身份安装时，数据包落在**执行 UAC 确认的账户**的
;      %LOCALAPPDATA% 下。单用户自装（首发场景）无影响；管理员代装他账号机器时，
;      实际用户首次运行会看到「数据包未部署」的业务层指引（errors.py），把
;      %LOCALAPPDATA%\AeroForge\data 拷到实际用户目录或设 AEROFORGE_DATA_DIR 即可。

; ── 可覆盖常量 ──────────────────────────────────────────────────────────────
!ifndef VERSION
  !define VERSION "0.7.1"
!endif
!ifndef CODE_DIR
  !define CODE_DIR "..\..\dist-desktop\AeroForge"
!endif
!ifndef DATA_DIR
  !define DATA_DIR "..\..\data"
!endif
!ifndef OUT_DIR
  !define OUT_DIR "dist"
!endif

!define PRODUCT_NAME "AeroForge"
!define PRODUCT_PUBLISHER "AeroForge 项目"
!define UNINST_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT_NAME}"
!define APP_KEY "Software\${PRODUCT_NAME}"

; ── 基础配置 ────────────────────────────────────────────────────────────────
Unicode true
; 默认装 Program Files（纯 ASCII，满足 R-34）；写 Program Files 需要管理员。
RequestExecutionLevel admin
SetCompressor /SOLID lzma

!include "MUI2.nsh"
!include "FileFunc.nsh"

Name "${PRODUCT_NAME} ${VERSION}"
OutFile "${OUT_DIR}\${PRODUCT_NAME}-${VERSION}-setup.exe"
InstallDir "$PROGRAMFILES64\${PRODUCT_NAME}"
InstallDirRegKey HKLM "${APP_KEY}" "InstallDir"
ShowInstDetails show
ShowUnInstDetails show

; ── 版本信息（资源管理器文件属性）────────────────────────────────────────
VIProductVersion "${VERSION}.0"
VIAddVersionKey ProductName "${PRODUCT_NAME} — 参数化航天器设计与评估平台"
VIAddVersionKey ProductVersion "${VERSION}"
VIAddVersionKey FileVersion "${VERSION}.0"
VIAddVersionKey FileDescription "${PRODUCT_NAME} 桌面安装程序"
VIAddVersionKey LegalCopyright "© ${PRODUCT_PUBLISHER}（平台源码私有；第三方许可见 docs/third-party-licenses.md）"
VIAddVersionKey CompanyName "${PRODUCT_PUBLISHER}"

; ── MUI 页面 ────────────────────────────────────────────────────────────────
!define MUI_ABORTWARNING
!define MUI_ICON "${NSISDIR}\Contrib\Graphics\Icons\modern-install.ico"
!define MUI_UNICON "${NSISDIR}\Contrib\Graphics\Icons\modern-uninstall.ico"
!define MUI_PAGE_CUSTOMFUNCTION_LEAVE ValidateInstallDir
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_COMPONENTS
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "SimpChinese"

; ── 安装器内辅助函数 ────────────────────────────────────────────────────────

; .onInit：统一 64 位注册表视图（x64 机器上 HKLM\SOFTWARE 写原生视图而非 WOW6432Node）；
; 检查正在运行的 AeroForge 实例，避免文件被占用导致安装残缺。
Function .onInit
  SetRegView 64
  nsExec::ExecToStack `cmd /c tasklist /FI "IMAGENAME eq AeroForge.exe" /NH 2>nul | find /I "AeroForge.exe"`
  Pop $0
  StrCmp $0 "0" 0 _no_run
  MessageBox MB_ICONEXCLAMATION|MB_OK \
    "检测到 AeroForge 正在运行。$\n请先退出 AeroForge（关闭其窗口），再重新运行本安装程序。"
  Abort
_no_run:
FunctionEnd

; IsASCIIPath <字符串> → 栈顶 "1"=纯 ASCII / "0"=含非 ASCII。
; 实现：按 CP 20127（us-ascii）做 WideCharToMultiByte，非 ASCII 字符会被替换成 '?'，
; 与 launcher.ascii_root() 的 str.encode("ascii") 语义一致（任何 >127 的码位都判不安全）。
Function IsASCIIPath
  Exch $R0                 ; 取参（原 $R0 压栈保存）
  System::Call 'kernel32::WideCharToMultiByte(i 20127, i 0, w "$R0", i -1, t .R1, i ${NSIS_MAX_STRLEN}, i 0, i 0) i .R2'
  StrCmp $R2 "0" _ascii_bad 0          ; 转换失败 → 按不安全处理
  StrCmp $R1 "$R0" _ascii_good _ascii_bad
_ascii_good:
  StrCpy $R0 "1"
  Goto _ascii_done
_ascii_bad:
  StrCpy $R0 "0"
_ascii_done:
  Exch $R0                 ; 结果压回栈顶，恢复原 $R0
FunctionEnd

; ValidateInstallDir：目录页离开回调——安装路径含非 ASCII 即拒绝（R-34，退出码 5 的前置拦截）
Function ValidateInstallDir
  Push $INSTDIR
  Call IsASCIIPath
  Pop $R0
  StrCmp $R0 "1" _dir_ok
  MessageBox MB_ICONEXCLAMATION|MB_OK \
    "安装路径含非 ASCII（中文等）字符：$\n$INSTDIR$\n$\nNASA CEA 的原生层无法从非 ASCII 路径加载数据表，$\n安装后启动必然失败（退出码 5）。$\n$\n请选择纯英文路径，例如：$\n  C:\Program Files\AeroForge$\n  D:\AeroForge"
  Abort
_dir_ok:
FunctionEnd

; ── 主程序（代码包，必选）─────────────────────────────────────────────────
Section "AeroForge 主程序（代码包）" SEC_MAIN
  SectionIn RO
  SetOutPath "$INSTDIR"
  File /r "${CODE_DIR}\*.*"

  ; 卸载器先落位再写注册表，保证「卸载入口」永远存在
  WriteUninstaller "$INSTDIR\uninstall.exe"

  CreateDirectory "$SMPROGRAMS\${PRODUCT_NAME}"
  CreateShortCut "$SMPROGRAMS\${PRODUCT_NAME}\${PRODUCT_NAME}.lnk" "$INSTDIR\AeroForge.exe"
  CreateShortCut "$SMPROGRAMS\${PRODUCT_NAME}\卸载 ${PRODUCT_NAME}.lnk" "$INSTDIR\uninstall.exe"

  ; 添加/删除程序注册（控制面板「应用」可见）
  WriteRegStr HKLM "${APP_KEY}" "InstallDir" "$INSTDIR"
  WriteRegStr HKLM "${UNINST_KEY}" "DisplayName" "${PRODUCT_NAME} — 参数化航天器设计与评估平台"
  WriteRegStr HKLM "${UNINST_KEY}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKLM "${UNINST_KEY}" "Publisher" "${PRODUCT_PUBLISHER}"
  WriteRegStr HKLM "${UNINST_KEY}" "DisplayIcon" "$INSTDIR\AeroForge.exe"
  WriteRegStr HKLM "${UNINST_KEY}" "UninstallString" "$INSTDIR\uninstall.exe"
  WriteRegDWORD HKLM "${UNINST_KEY}" "NoModify" 1
  WriteRegDWORD HKLM "${UNINST_KEY}" "NoRepair" 1
  ; EstimatedSize：主程序实际落盘体积（KB，含数据包修正见 SEC_DATA）
  ${GetSize} "$INSTDIR" "/S=0B" $0 $1 $2
  IntOp $0 $0 / 1024
  WriteRegDWORD HKLM "${UNINST_KEY}" "EstimatedSize" "$0"

  ; WebView2 运行时检测与引导（R-33 / M7）：口径与 desktop/launcher.py 的三处注册表一致。
  Call CheckWebView2
SectionEnd

; ── 数据包（可选，默认勾选）───────────────────────────────────────────────
Section "数据包（GCAT 目录库 + CEA 预计算表）" SEC_DATA
  SetOutPath "$LOCALAPPDATA\AeroForge\data"
  File /r "${DATA_DIR}\cea"
  File /r "${DATA_DIR}\snapshots"
  File "${DATA_DIR}\aeroforge.db"
SectionEnd

; ── 桌面快捷方式（可选，默认勾选）─────────────────────────────────────────
Section "桌面快捷方式" SEC_DESK
  CreateShortCut "$DESKTOP\${PRODUCT_NAME}.lnk" "$INSTDIR\AeroForge.exe"
SectionEnd

; ── WebView2 检测（引导安装，不捆绑）──────────────────────────────────────
Function CheckWebView2
  ; Microsoft 官方 EdgeUpdate 客户端 GUID（与 launcher._WEBVIEW2_CLIENT_ID 一致）
  !define WEBVIEW2_GUID "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
  SetRegView 64
  ClearErrors
  ReadRegStr $R0 HKLM "SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\${WEBVIEW2_GUID}" "pv"
  IfErrors 0 _wv_found
  ClearErrors
  ReadRegStr $R0 HKLM "SOFTWARE\Microsoft\EdgeUpdate\Clients\${WEBVIEW2_GUID}" "pv"
  IfErrors 0 _wv_found
  ClearErrors
  ReadRegStr $R0 HKCU "SOFTWARE\Microsoft\EdgeUpdate\Clients\${WEBVIEW2_GUID}" "pv"
  IfErrors 0 _wv_found
  ; 三处都查不到：引导安装（Evergreen 引导器 / 官方页），不强制捆绑
  MessageBox MB_YESNO|MB_ICONQUESTION \
    "未检测到 Microsoft Edge WebView2 运行时，AeroForge 桌面窗口无法创建。$\n$\n是否现在打开官方下载页面安装？（安装完成后重新运行 AeroForge 即可）$\n$\n官方页：https://developer.microsoft.com/microsoft-edge/webview2/$\nEvergreen 引导器直达：https://go.microsoft.com/fwlink/?linkid=2124703" \
    IDYES _wv_open IDNO _wv_skip
_wv_open:
  ExecShell "open" "https://developer.microsoft.com/microsoft-edge/webview2/"
_wv_skip:
  Return
_wv_found:
  StrCmp $R0 "" 0 _wv_done
  ; pv 值为空串也视为缺失
  MessageBox MB_ICONEXCLAMATION \
    "检测到 WebView2 注册表项但版本值为空，运行时可能不完整。$\n建议从官方页重装 Evergreen 运行时：$\nhttps://developer.microsoft.com/microsoft-edge/webview2/"
_wv_done:
FunctionEnd

; ── 卸载 ───────────────────────────────────────────────────────────────────
Section Uninstall
  ; 1) 快捷方式
  Delete "$SMPROGRAMS\${PRODUCT_NAME}\${PRODUCT_NAME}.lnk"
  Delete "$SMPROGRAMS\${PRODUCT_NAME}\卸载 ${PRODUCT_NAME}.lnk"
  RMDir "$SMPROGRAMS\${PRODUCT_NAME}"
  Delete "$DESKTOP\${PRODUCT_NAME}.lnk"

  ; 2) 代码包与卸载器自身
  RMDir /r "$INSTDIR"

  ; 3) 注册表
  DeleteRegKey HKLM "${UNINST_KEY}"
  DeleteRegKey HKLM "${APP_KEY}"

  ; 4) 数据与用户数据——默认保留（artifacts 内容寻址缓存 / 母线存档 / 阈值配置属用户数据），
  ;    弹窗确认后才删。默认按钮落在「保留」上（DEFBUTTON2）。
  MessageBox MB_YESNO|MB_ICONQUESTION|MB_DEFBUTTON2 \
    "是否同时删除数据与用户数据目录？$\n$LOCALAPPDATA\AeroForge$\n$\n该目录包含：GCAT 数据包、CEA 预计算表、母线存档、阈值配置、$\n内容寻址产物缓存（artifacts，可由参数重建）。$\n$\n【默认：保留】重新安装时无需重新部署数据。" \
    IDNO _keep_data
  RMDir /r "$LOCALAPPDATA\AeroForge"
_keep_data:
SectionEnd
