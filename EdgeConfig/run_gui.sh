#!/bin/sh
# EdgeConfig GUI 실행
#
# **sudo 를 쓰지 않는다.** 사용자가 dialout·gpio 그룹에 있으면 시리얼과 GPIO 에
# 그대로 접근할 수 있고, sudo 로 올리면 오히려 X 인증이 꼬인다.
#
# SSH 로 들어와 쓸 때는 Pi 에 붙은 화면(:0)으로 창이 나간다.
cd "$(dirname "$0")"
export DISPLAY="${DISPLAY:-:0}"
exec python3 -m edgeconfig.cli gui "$@"
