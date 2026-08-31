# Public measurement Dashboard / 公開量測 Dashboard

`ensure_server.ps1` 將 Approval/Dashboard 的本機 `127.0.0.1:8766` 透過 Cloudflare
HTTPS quick tunnel forward 到公開網址。MCP 固定留在 `127.0.0.1:8765`，不會被
forward。公開端只提供共用首頁、審閱、核准、結果圖、歷史選單與四種控制狀態。

`ensure_server.ps1` forwards local Approval/Dashboard port `127.0.0.1:8766`
through a Cloudflare HTTPS quick tunnel. MCP remains on `127.0.0.1:8765` and is
never forwarded. The public surface exposes only the shared Home, review,
approval, result plots, history selection, and four operator control states.

每個 service lifetime 都有只留在量測電腦檔案系統內的 256-bit access secret。
entry response 的初次 URL 只帶獨立、短效、一次性的 bootstrap code；第一次成功
開啟後，server 會為該瀏覽器建立 12 小時、`HttpOnly`、`Secure`、
`SameSite=Strict` 的個別 device cookie，並立即從網址移除 code。初次 URL 用過後
不能再拿去開手機，這是預期的 replay 防護，不是 Dashboard 故障。

Each service lifetime keeps a random 256-bit access secret on the lab PC. The
initial entry URL carries only a separate short-lived, single-use bootstrap code.
After exchange, the server creates a unique 12-hour `HttpOnly`, `Secure`,
`SameSite=Strict` device cookie and removes the code from the address bar. Reusing
that initial URL on a phone is intentionally rejected as a replay.

要連接手機或第二個瀏覽器，請在量測電腦開啟
`http://127.0.0.1:8765/operator`，輸入裝置名稱並建立 10 分鐘的一次性配對連結，
再把該連結傳到指定裝置。每台裝置取得不同 cookie，可在同一本機頁面個別撤銷；
原桌面不會因手機配對或撤銷而失效。不要公開、截圖或長期保存 bootstrap／配對
連結。Quick tunnel hostname 在服務重建後可能改變，請使用當次 entry response。

To connect a phone or second browser, open `http://127.0.0.1:8765/operator` on
the lab PC, name the device, and create a ten-minute single-use pairing link.
Each device receives an independent cookie that can be revoked locally without
invalidating the desktop. Do not publish or retain bootstrap/pairing links. The
quick-tunnel hostname can change after restart, so use the current entry result.

手機不需 VPN App。它只需要一般瀏覽器開啟 public URL；AI 對話仍透過手機 remote
回量測電腦的既有 agent task。若 tunnel 建立失敗，JY 不會改用 LAN binding，也
不會把 MCP 暴露到公網。

No VPN app is required on the phone; a normal browser opens the public URL. The
AI conversation remains a remote view of the desktop agent task. If the tunnel
cannot be established, JY does not fall back to a LAN listener and never exposes
MCP publicly.

硬體鎖 recovery 也在同一本機 operator console，但永遠不經 public tunnel。
只有所有軟體檢查通過、現場人員勾選已確認儀器無 active job/output，並輸入完整
確認句時才會封存 lock；不能從手機遠端繞過現場檢查。

Hardware-lock recovery is also available in that local console but never through
the public tunnel. It still requires all software checks, physical inspection,
the hardware attestation, and the exact confirmation phrase.
