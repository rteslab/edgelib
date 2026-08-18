/*
 * transport — RS-485 반이중 링크 (시리얼 + DIR GPIO)
 *
 * Copyright (c) 2026 RTES Co., Ltd. All rights reserved.
 */

#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif

#include "transport.h"
#include "frame.h"

#include <errno.h>
#include <fcntl.h>
#include <linux/gpio.h>
#include <linux/serial.h>
#include <stdio.h>
#include <string.h>
#include <sys/ioctl.h>
#include <termios.h>
#include <time.h>
#include <unistd.h>

/* asm-generic/ioctls.h — 헤더에 없을 수도 있어 값을 적어 둔다 */
#ifndef TIOCGRS485
#define TIOCGRS485 0x542E
#endif
#ifndef TIOCSRS485
#define TIOCSRS485 0x542F
#endif

/* ── 시간 ────────────────────────────────────────────────────────────────── */
uint64_t edge_now_us(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000u + (uint64_t)(ts.tv_nsec / 1000);
}

void edge_sleep_us(uint32_t us)
{
    if (us == 0u) { return; }

    /* 짧은 대기는 바쁘게 기다린다. nanosleep 은 깨우기까지 수십 µs 가 더 걸려,
       프레임 사이 4 µs 가드에 쓰면 사이클 예산이 통째로 어긋난다. */
    if (us < 200u) {
        const uint64_t end = edge_now_us() + us;
        while (edge_now_us() < end) { }
        return;
    }

    struct timespec ts;
    ts.tv_sec  = (time_t)(us / 1000000u);
    ts.tv_nsec = (long)(us % 1000000u) * 1000L;
    while (nanosleep(&ts, &ts) == -1 && errno == EINTR) { }
}

/* ── 보레이트 ────────────────────────────────────────────────────────────── */
static speed_t baud_const(int baud)
{
    switch (baud) {
    case 9600:    return B9600;
    case 19200:   return B19200;
    case 38400:   return B38400;
    case 57600:   return B57600;
    case 115200:  return B115200;
    case 230400:  return B230400;
    case 460800:  return B460800;
    case 921600:  return B921600;
    case 1000000: return B1000000;
    case 1500000: return B1500000;
    case 2000000: return B2000000;
    case 2500000: return B2500000;
    case 3000000: return B3000000;
    default:      return 0;
    }
}

/* ── GPIO (문자 장치 v2 — libgpiod 를 요구하지 않는다) ───────────────────── */
static int gpio_claim(edge_transport_t *tr, const char *chip, int line)
{
    const int cfd = open(chip, O_RDWR | O_CLOEXEC);
    if (cfd < 0) {
        snprintf(tr->err, sizeof tr->err, "cannot open %s: %s",
                 chip, strerror(errno));
        return -1;
    }

    struct gpio_v2_line_request req;
    memset(&req, 0, sizeof req);
    req.offsets[0] = (uint32_t)line;
    req.num_lines  = 1u;
    req.config.flags = GPIO_V2_LINE_FLAG_OUTPUT;
    /* **자기 PID 를 실어 둔다.** 커널이 이 이름을 그대로 들고 있다가 다음
       사람에게 보여 준다. "edgelib" 만 적어 두면 "edgelib 을 쓰는 무언가"
       까지밖에 모르는데, 이 버스는 한 번에 하나만 쥐므로 정확히 누구인지가
       곧 해결책이다. */
    snprintf(req.consumer, sizeof req.consumer, "edgelib[%ld]", (long)getpid());

    if (ioctl(cfd, GPIO_V2_GET_LINE_IOCTL, &req) < 0) {
        const int why = errno;

        /* **누가 쥐고 있는지까지 말해 준다.** "바쁘다"만으로는 사용자가 할 수
           있는 일이 없다. 커널이 그 줄을 잡은 쪽의 consumer 이름을 들고 있으므로
           물어보면 되고, GUI 인지 다른 응용인지가 그것으로 갈린다. */
        struct gpio_v2_line_info info;
        memset(&info, 0, sizeof info);
        info.offset = (uint32_t)line;
        if (why == EBUSY
            && ioctl(cfd, GPIO_V2_GET_LINEINFO_IOCTL, &info) == 0
            && info.consumer[0] != 0) {
            snprintf(tr->err, sizeof tr->err,
                     "cannot claim GPIO%d: held by %.32s - the bus takes "
                     "one program at a time (fuser -v %.32s)",
                     line, info.consumer, tr->port_name);
        } else {
            snprintf(tr->err, sizeof tr->err,
                     "cannot claim GPIO%d: %s - another process may hold the bus",
                     line, strerror(why));
        }
        close(cfd);
        return -1;
    }
    close(cfd);                       /* 라인 fd 가 따로 살아 있다 */
    tr->gpio_fd  = req.fd;
    tr->dir_line = 0u;                /* 요청 안에서의 인덱스 */
    return 0;
}

static void gpio_set(const edge_transport_t *tr, int on)
{
    if (tr->gpio_fd < 0) { return; }
    struct gpio_v2_line_values v;
    v.mask = 1u;
    v.bits = on ? 1u : 0u;
    (void)ioctl(tr->gpio_fd, GPIO_V2_LINE_SET_VALUES_IOCTL, &v);
}

/* ── 열기 · 닫기 ─────────────────────────────────────────────────────────── */
static int enable_kernel_rs485(edge_transport_t *tr)
{
    struct serial_rs485 rs;
    memset(&rs, 0, sizeof rs);
    if (ioctl(tr->fd, TIOCGRS485, &rs) < 0) {
        snprintf(tr->err, sizeof tr->err,
                 "cannot read RS485 mode: %s - without it the frame tail is cut",
                 strerror(errno));
        return -1;
    }
    if ((rs.flags & SER_RS485_ENABLED) == 0u) {
        rs.flags |= SER_RS485_ENABLED | SER_RS485_RTS_ON_SEND;
        if (ioctl(tr->fd, TIOCSRS485, &rs) < 0) {
            snprintf(tr->err, sizeof tr->err,
                     "cannot enable kernel RS485 mode: %s", strerror(errno));
            return -1;
        }
    }
    return 0;
}

int edge_transport_open(edge_transport_t *tr, const char *port, int baud,
                        const char *chip, int dir_gpio)
{
    memset(tr, 0, sizeof *tr);
    tr->fd = -1;
    tr->gpio_fd = -1;
    tr->baud = baud;

    /* 마지막 비트가 선로를 떠날 때까지 — 정지비트 여유를 넉넉히 본다 */
    tr->guard_us = (uint32_t)((12.0 / (double)baud) * 1e6) + 1u;
    tr->idle_us  = 500u;
    snprintf(tr->port_name, sizeof tr->port_name, "%s", port ? port : "?");

    const speed_t sp = baud_const(baud);
    if (sp == 0) {
        snprintf(tr->err, sizeof tr->err, "unsupported baud %d", baud);
        return -1;
    }

    tr->fd = open(port, O_RDWR | O_NOCTTY | O_NONBLOCK | O_CLOEXEC);
    if (tr->fd < 0) {
        snprintf(tr->err, sizeof tr->err, "cannot open %s: %s",
                 port, strerror(errno));
        return -1;
    }

    struct termios tio;
    if (tcgetattr(tr->fd, &tio) < 0) {
        snprintf(tr->err, sizeof tr->err, "tcgetattr: %s", strerror(errno));
        edge_transport_close(tr);
        return -1;
    }
    cfmakeraw(&tio);
    tio.c_cflag |= (CLOCAL | CREAD);
    tio.c_cflag &= (tcflag_t)~CRTSCTS;
    tio.c_cc[VMIN]  = 0;
    tio.c_cc[VTIME] = 0;
    cfsetispeed(&tio, sp);
    cfsetospeed(&tio, sp);
    if (tcsetattr(tr->fd, TCSANOW, &tio) < 0) {
        snprintf(tr->err, sizeof tr->err, "tcsetattr: %s", strerror(errno));
        edge_transport_close(tr);
        return -1;
    }

    if (enable_kernel_rs485(tr) != 0) {
        edge_transport_close(tr);
        return -1;
    }

    if (dir_gpio >= 0) {
        if (gpio_claim(tr, chip ? chip : EDGE_DEFAULT_CHIP, dir_gpio) != 0) {
            edge_transport_close(tr);
            return -1;
        }
        gpio_set(tr, 0);
    }

    tcflush(tr->fd, TCIOFLUSH);
    return 0;
}

void edge_transport_close(edge_transport_t *tr)
{
    if (tr->gpio_fd >= 0) {
        gpio_set(tr, 0);
        close(tr->gpio_fd);
        tr->gpio_fd = -1;
    }
    if (tr->fd >= 0) {
        close(tr->fd);
        tr->fd = -1;
    }
}

/* ── 송수신 ──────────────────────────────────────────────────────────────── */

/**
 * @brief 송신이 선로를 떠날 때까지. **`tcdrain()` 을 쓰지 않는다.**
 *
 * WHY: 커널 6.18 의 pl011 에서 `tcdrain()` 이 같은 5 B 프레임에 5 µs ~ 48 ms 로
 * 요동한다 (2026-08-18, CM4 실측). 6.12 에서는 RS-485 모드가 켜져 있으면 마지막
 * 비트까지만 정확히 기다렸고, `docs/backplane/backplane_iol_board.md` §9-2 는 그
 * 동작을 전제로 쓰여 있다. 6.18 은 그 전제를 깬다.
 *
 * 그 시간만큼 DE 가 High 로 남으면 트랜시버의 `/RE` 가 묶여 있어, 슬레이브가
 * `T_TURNAROUND`(1 ms) 에 보낸 응답이 통째로 사라진다. 증상은 "보내지기는 하는데
 * 한 바이트도 안 들어온다" 로, 배선이나 보레이트를 의심하게 만든다.
 *
 * 그래서 커널에게 묻는 것은 **출력 큐가 비었는가** 하나뿐이고 (`TIOCOUTQ`),
 * 하드웨어 FIFO 에 남은 몫은 보레이트에서 계산해 기다린다. 3 Mbaud 에서 113 µs 로,
 * 턴어라운드 1 ms 안에 넉넉히 들어온다.
 */
static void drain_tx(edge_transport_t *tr)
{
    const uint64_t limit = edge_now_us() + 20000u;   /* 큐가 안 비어도 손을 뗀다 */
    for (;;) {
        int q = 0;
        if (ioctl(tr->fd, TIOCOUTQ, &q) < 0) { break; }
        if (q == 0) { break; }
        if (edge_now_us() >= limit) { break; }
    }
    /* PL011 FIFO 32 B + 시프트 레지스터 2 B 분, 10 비트/바이트 */
    edge_sleep_us((uint32_t)((34.0 * 10.0 / (double)tr->baud) * 1e6) + 1u);
}

static int send_raw(edge_transport_t *tr, const uint8_t *buf, size_t n)
{
    gpio_set(tr, 1);

    size_t off = 0u;
    while (off < n) {
        const ssize_t w = write(tr->fd, buf + off, n - off);
        if (w < 0) {
            if (errno == EAGAIN || errno == EINTR) { continue; }
            gpio_set(tr, 0);                  /* 예외가 나도 선을 놓는다 */
            snprintf(tr->err, sizeof tr->err, "write: %s", strerror(errno));
            return -1;
        }
        off += (size_t)w;
    }
    drain_tx(tr);
    edge_sleep_us(tr->guard_us);
    gpio_set(tr, 0);
    return 0;
}

static int recv_frame(edge_transport_t *tr, uint8_t *buf, size_t cap,
                      uint32_t first_timeout_us)
{
    size_t n = 0u;
    uint64_t deadline = edge_now_us() + first_timeout_us;

    for (;;) {
        if (n < cap) {
            const ssize_t r = read(tr->fd, buf + n, cap - n);
            if (r > 0) {
                n += (size_t)r;
                deadline = edge_now_us() + tr->idle_us;
                continue;
            }
            if (r < 0 && errno != EAGAIN && errno != EINTR) {
                snprintf(tr->err, sizeof tr->err, "read: %s", strerror(errno));
                return -1;
            }
        }
        if (edge_now_us() >= deadline) { return (int)n; }
        edge_sleep_us(50u);
    }
}

int edge_transport_xact(edge_transport_t *tr, uint8_t addr,
                        const uint8_t *pdu, size_t pdu_len,
                        uint8_t *rsp, size_t rsp_cap, uint32_t timeout_us)
{
    uint8_t frame[BP_FRAME_MAX];
    const size_t nf = bp_frame_build(addr, pdu, pdu_len, frame, sizeof frame);
    if (nf == 0u) { return -1; }

    tcflush(tr->fd, TCIFLUSH);
    if (send_raw(tr, frame, nf) != 0) { return -1; }

    uint8_t raw[BP_FRAME_MAX];
    const int got = recv_frame(tr, raw, sizeof raw, timeout_us);
    if (got < 0)  { return -1; }
    if (got == 0) { return 0; }               /* 응답 없음 */

    uint8_t ra = 0u;
    const uint8_t *body = NULL;
    size_t blen = 0u;
    const int used = bp_frame_parse(raw, (size_t)got, &ra, &body, &blen);
    if (used < 0) { return 0; }               /* 깨진 것은 못 받은 것과 같다 */

    if (blen > rsp_cap) { blen = rsp_cap; }
    memcpy(rsp, body, blen);
    return (int)blen;
}

int edge_transport_broadcast(edge_transport_t *tr,
                             const uint8_t *pdu, size_t pdu_len)
{
    uint8_t frame[BP_FRAME_MAX];
    const size_t nf = bp_frame_build(BP_ADDR_BROADCAST, pdu, pdu_len,
                                     frame, sizeof frame);
    if (nf == 0u) { return -1; }
    if (send_raw(tr, frame, nf) != 0) { return -1; }
    edge_sleep_us(5000u);                     /* 모두가 받아 처리할 틈 */
    return 0;
}
