<h1 align="center">GPU 모니터</h1>

<p align="center">
  <b>어느 GPU 서버가 지금 무엇에 쓰이는지 한 화면에서 봅니다.</b><br>
  Claude Code가 GPU 작업이 무엇인지 알아내고, 진행률과 종료 시각은 그 작업의 로그에서 바로 읽습니다.
</p>

<p align="center"><a href="README.md">English</a> · <b>한국어</b></p>

<p align="center"><img src="docs/ko/demo.gif" alt="대시보드: 서버별 작업·진행률·종료 시각과 그래프" width="100%"></p>

## 설치

**1. hub** — 팀에 하나

```bash
git clone https://github.com/zoo3323/gpu-monitor.git && cd gpu-monitor
./install.sh hub --lang ko
```

<sub>포트는 기본 8787이고, 이미 쓰이고 있으면 빈 포트를 고릅니다 · 직접 정하려면 <code>--port 9000</code></sub>

**2. GPU 서버마다** — hub가 알려 준 한 줄을 붙여 넣습니다

```bash
curl -fsSL --noproxy '*' "http://HUB:PORT/install.sh?t=TOKEN" | bash
```

<sub>hub에 SSH로만 닿는 서버는 <code>./install.sh agent --ssh 사용자@hub --token TOKEN</code>으로 터널을 띄워 붙습니다 · 리눅스와 WSL2</sub>

**3. AI 작업 식별** — 서버마다 한 번

```bash
claude auth login
```

<p align="center"><img src="docs/ko/install.gif" alt="hub에서 ./install.sh hub, GPU 서버에서 한 줄 설치" width="100%"></p>

## 화면

<table>
  <tr>
    <td width="70%"><img src="docs/ko/light.png" alt="밝은 테마"></td>
    <td><img src="docs/ko/mobile.png" alt="폰, 다크 테마"></td>
  </tr>
  <tr>
    <td colspan="2"><img src="docs/ko/detail.png" alt="머신 상세: 무슨 작업인지, 다음 작업, loss 곡선"></td>
  </tr>
</table>

<sub>Linux + NVIDIA · Python 3.7 이상 · 작업 식별에 <a href="https://claude.com/claude-code">Claude Code</a>(선택) · English / 한국어 · <a href="docs/DETAILS.ko.md">자세히</a> · MIT</sub>
