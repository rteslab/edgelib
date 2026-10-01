/*
 * config — 커미셔닝 JSON 파서
 *
 * Copyright (c) 2026 RTES Co., Ltd. All rights reserved.
 *
 * 파서는 값 트리를 한 번 만들고 거기서 꺼내 쓴다. 스트리밍으로 짜면 필드 순서에
 * 의존하게 되는데, 사람이 손으로 고치는 파일에 그 제약을 두고 싶지 않다.
 */

#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif

#include "config.h"

#include "transport.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* ── 최소 JSON 값 트리 ───────────────────────────────────────────────────── */
typedef enum { JNULL, JBOOL, JNUM, JSTR, JARR, JOBJ } jtype_e;

typedef struct jval {
    jtype_e type;
    double  num;
    char   *str;           /* JSTR — 소유한다 */
    struct jval  **items;  /* JARR · JOBJ 의 값 */
    char        **keys;    /* JOBJ 의 키 */
    int           n;
} jval_t;

typedef struct {
    const char *p;
    const char *end;
    char        err[128];
} jparse_t;

static jval_t *jparse_value(jparse_t *J);

static void jfree(jval_t *v)
{
    if (v == NULL) { return; }
    for (int i = 0; i < v->n; i++) {
        jfree(v->items[i]);
        if (v->keys != NULL) { free(v->keys[i]); }
    }
    free(v->items);
    free(v->keys);
    free(v->str);
    free(v);
}

static void jskip(jparse_t *J)
{
    while (J->p < J->end) {
        const char c = *J->p;
        if (c == ' ' || c == '\t' || c == '\n' || c == '\r') { J->p++; continue; }
        break;
    }
}

static jval_t *jnew(jtype_e t)
{
    jval_t *v = calloc(1, sizeof *v);
    if (v != NULL) { v->type = t; }
    return v;
}

static int jpush(jval_t *v, char *key, jval_t *child)
{
    jval_t **ni = realloc(v->items, (size_t)(v->n + 1) * sizeof *ni);
    if (ni == NULL) { return -1; }
    v->items = ni;
    if (key != NULL) {
        char **nk = realloc(v->keys, (size_t)(v->n + 1) * sizeof *nk);
        if (nk == NULL) { return -1; }
        v->keys = nk;
        v->keys[v->n] = key;
    }
    v->items[v->n] = child;
    v->n++;
    return 0;
}

static char *jparse_string(jparse_t *J)
{
    if (J->p >= J->end || *J->p != '"') {
        snprintf(J->err, sizeof J->err, "expected string");
        return NULL;
    }
    J->p++;

    size_t cap = 32u, n = 0u;
    char *s = malloc(cap);
    if (s == NULL) { return NULL; }

    while (J->p < J->end && *J->p != '"') {
        char c = *J->p++;
        if (c == '\\' && J->p < J->end) {
            const char e = *J->p++;
            switch (e) {
            case 'n':  c = '\n'; break;
            case 't':  c = '\t'; break;
            case 'r':  c = '\r'; break;
            case 'b':  c = '\b'; break;
            case 'f':  c = '\f'; break;
            case 'u': {
                /* \uXXXX — 설정 파일에 쓰이는 것은 사실상 ASCII 라 그 범위만 푼다.
                   나머지는 원문을 그대로 두어 정보를 잃지 않는다. */
                if (J->end - J->p >= 4) {
                    char hex[5] = {0};
                    memcpy(hex, J->p, 4);
                    const long cp = strtol(hex, NULL, 16);
                    J->p += 4;
                    c = (cp < 0x80) ? (char)cp : '?';
                } else {
                    c = '?';
                }
                break;
            }
            default: c = e; break;
            }
        }
        if (n + 1u >= cap) {
            cap *= 2u;
            char *ns = realloc(s, cap);
            if (ns == NULL) { free(s); return NULL; }
            s = ns;
        }
        s[n++] = c;
    }
    if (J->p >= J->end) {
        snprintf(J->err, sizeof J->err, "unterminated string");
        free(s);
        return NULL;
    }
    J->p++;                       /* 닫는 따옴표 */
    s[n] = '\0';
    return s;
}

static jval_t *jparse_value(jparse_t *J)
{
    jskip(J);
    if (J->p >= J->end) {
        snprintf(J->err, sizeof J->err, "unexpected end");
        return NULL;
    }

    const char c = *J->p;

    if (c == '{' || c == '[') {
        const int is_obj = (c == '{');
        jval_t *v = jnew(is_obj ? JOBJ : JARR);
        if (v == NULL) { return NULL; }
        J->p++;
        jskip(J);
        if (J->p < J->end && *J->p == (is_obj ? '}' : ']')) { J->p++; return v; }

        for (;;) {
            jskip(J);
            char *key = NULL;
            if (is_obj) {
                key = jparse_string(J);
                if (key == NULL) { jfree(v); return NULL; }
                jskip(J);
                if (J->p >= J->end || *J->p != ':') {
                    snprintf(J->err, sizeof J->err, "expected ':'");
                    free(key); jfree(v); return NULL;
                }
                J->p++;
            }
            jval_t *child = jparse_value(J);
            if (child == NULL) { free(key); jfree(v); return NULL; }
            if (jpush(v, key, child) != 0) {
                free(key); jfree(child); jfree(v); return NULL;
            }
            jskip(J);
            if (J->p < J->end && *J->p == ',') { J->p++; continue; }
            if (J->p < J->end && *J->p == (is_obj ? '}' : ']')) { J->p++; break; }
            snprintf(J->err, sizeof J->err, "expected ',' or close");
            jfree(v);
            return NULL;
        }
        return v;
    }

    if (c == '"') {
        char *s = jparse_string(J);
        if (s == NULL) { return NULL; }
        jval_t *v = jnew(JSTR);
        if (v == NULL) { free(s); return NULL; }
        v->str = s;
        return v;
    }

    if (strncmp(J->p, "true", 4u) == 0) {
        J->p += 4; jval_t *v = jnew(JBOOL); if (v) { v->num = 1.0; } return v;
    }
    if (strncmp(J->p, "false", 5u) == 0) {
        J->p += 5; jval_t *v = jnew(JBOOL); if (v) { v->num = 0.0; } return v;
    }
    if (strncmp(J->p, "null", 4u) == 0) {
        J->p += 4; return jnew(JNULL);
    }

    char *endp = NULL;
    const double d = strtod(J->p, &endp);
    if (endp == J->p) {
        snprintf(J->err, sizeof J->err, "bad value near '%.12s'", J->p);
        return NULL;
    }
    J->p = endp;
    jval_t *v = jnew(JNUM);
    if (v != NULL) { v->num = d; }
    return v;
}

/* ── 꺼내 쓰기 ───────────────────────────────────────────────────────────── */
static const jval_t *jget(const jval_t *o, const char *key)
{
    if (o == NULL || o->type != JOBJ) { return NULL; }
    for (int i = 0; i < o->n; i++) {
        if (strcmp(o->keys[i], key) == 0) { return o->items[i]; }
    }
    return NULL;
}

static double jnum(const jval_t *o, const char *key, double dflt)
{
    const jval_t *v = jget(o, key);
    return (v != NULL && (v->type == JNUM || v->type == JBOOL)) ? v->num : dflt;
}

static void jstr(const jval_t *o, const char *key, char *dst, size_t cap)
{
    const jval_t *v = jget(o, key);
    if (v != NULL && v->type == JSTR) {
        snprintf(dst, cap, "%s", v->str);
    }
}

/* ── 포트 모드 이름 ──────────────────────────────────────────────────────── */
static uint8_t mode_from_name(const char *s)
{
    if (s == NULL) { return EDGE_PM_DEACTIVATED; }
    if (strcmp(s, "IOL_AUTOSTART") == 0) { return EDGE_PM_IOL_AUTOSTART; }
    if (strcmp(s, "IOL_MANUAL") == 0)    { return EDGE_PM_IOL_MANUAL; }
    if (strcmp(s, "DI_CQ") == 0)         { return EDGE_PM_DI_CQ; }
    if (strcmp(s, "DO_CQ") == 0)         { return EDGE_PM_DO_CQ; }
    return EDGE_PM_DEACTIVATED;
}

/* ── 공개 ────────────────────────────────────────────────────────────────── */
void edge_config_defaults(edge_config_t *cfg)
{
    memset(cfg, 0, sizeof *cfg);
    snprintf(cfg->port, sizeof cfg->port, "%s", EDGE_DEFAULT_PORT);
    snprintf(cfg->chip, sizeof cfg->chip, "%s", EDGE_DEFAULT_CHIP);
    cfg->baud          = 3000000;
    cfg->dir_gpio      = 22;
    cfg->turnaround_us = 1000u;
    cfg->cycle_us      = 100000u;
    cfg->cycle_min_us  = 0u;
}

int edge_config_load(edge_config_t *cfg, const char *path,
                     char *err, size_t err_cap)
{
    edge_config_defaults(cfg);

    FILE *f = fopen(path, "rb");
    if (f == NULL) {
        if (err) { snprintf(err, err_cap, "cannot open %s", path); }
        return -1;
    }
    fseek(f, 0, SEEK_END);
    const long sz = ftell(f);
    fseek(f, 0, SEEK_SET);
    if (sz <= 0 || sz > 8 * 1024 * 1024) {
        if (err) { snprintf(err, err_cap, "config file size %ld is not sane", sz); }
        fclose(f);
        return -1;
    }
    char *text = malloc((size_t)sz + 1u);
    if (text == NULL) { fclose(f); return -1; }
    const size_t rd = fread(text, 1u, (size_t)sz, f);
    fclose(f);
    text[rd] = '\0';

    jparse_t J = { text, text + rd, {0} };
    jval_t *root = jparse_value(&J);
    free(text);
    if (root == NULL) {
        if (err) { snprintf(err, err_cap, "JSON: %s", J.err); }
        return -1;
    }
    if (root->type != JOBJ) {
        if (err) { snprintf(err, err_cap, "JSON: top level is not an object"); }
        jfree(root);
        return -1;
    }

    /* ── bus ─────────────────────────────────────────────────────────────── */
    const jval_t *bus = jget(root, "bus");
    if (bus != NULL) {
        jstr(bus, "port", cfg->port, sizeof cfg->port);
        jstr(bus, "chip", cfg->chip, sizeof cfg->chip);
        cfg->baud          = (int)jnum(bus, "baud", cfg->baud);
        cfg->dir_gpio      = (int)jnum(bus, "dir_gpio", cfg->dir_gpio);
        cfg->turnaround_us = (uint32_t)jnum(bus, "turnaround_us", cfg->turnaround_us);
        cfg->cycle_us      = (uint32_t)jnum(bus, "cycle_us", cfg->cycle_us);
        cfg->cycle_min_us  = (uint32_t)jnum(bus, "cycle_min_us", 0);
    }

    /* ── process_image — 크기만 쓴다. 항목 해석은 생성 코드의 몫이다 ─────── */
    const jval_t *pi = jget(root, "process_image");
    if (pi != NULL) {
        cfg->image_in_bytes  = (uint16_t)jnum(jget(pi, "in"),  "bytes", 0);
        cfg->image_out_bytes = (uint16_t)jnum(jget(pi, "out"), "bytes", 0);
    }

    /* ── nodes ───────────────────────────────────────────────────────────── */
    const jval_t *nodes = jget(root, "nodes");
    if (nodes == NULL || nodes->type != JARR || nodes->n == 0) {
        if (err) { snprintf(err, err_cap, "config has no nodes"); }
        jfree(root);
        return -1;
    }
    if (nodes->n > EDGE_MAX_NODES) {
        if (err) {
            snprintf(err, err_cap, "%d nodes exceeds the %d the library holds",
                     nodes->n, EDGE_MAX_NODES);
        }
        jfree(root);
        return -1;
    }

    uint16_t in_off = 0u, out_off = 0u;
    for (int i = 0; i < nodes->n; i++) {
        const jval_t *nj = nodes->items[i];
        edge_node_t *n = &cfg->nodes[i];
        memset(n, 0, sizeof *n);

        n->address = (uint8_t)jnum(nj, "address", 0);
        jstr(nj, "type", n->type_name, sizeof n->type_name);

        char am[16] = {0};
        jstr(nj, "address_mode", am, sizeof am);
        cfg->addr_fixed[i] = (uint8_t)(strcmp(am, "fixed") == 0 ? 1 : 0);

        const jval_t *uid = jget(nj, "uid");
        if (uid != NULL) {
            n->category = (uint8_t)jnum(uid, "category", 0);
            n->model    = (uint8_t)jnum(uid, "model", 0);
            n->variant  = (uint8_t)jnum(uid, "variant", 0);
            n->hw_rev   = (uint8_t)jnum(uid, "hw_rev", 0);
            jstr(uid, "serial", n->serial, sizeof n->serial);
        }

        n->pd_in  = (uint16_t)jnum(nj, "pd_in", 0);
        n->pd_out = (uint16_t)jnum(nj, "pd_out", 0);
        n->image_in_off  = in_off;
        n->image_out_off = out_off;
        in_off  = (uint16_t)(in_off  + n->pd_in);
        out_off = (uint16_t)(out_off + n->pd_out);

        const jval_t *ports = jget(nj, "ports");
        if (ports != NULL && ports->type == JARR) {
            for (int k = 0; k < ports->n && k < EDGE_MAX_PORTS; k++) {
                const jval_t *pj = ports->items[k];
                edge_port_t *p = &n->ports[n->port_count];
                p->port   = (uint8_t)jnum(pj, "port", (double)(k + 1));
                p->pd_in  = (uint8_t)jnum(pj, "pd_in", 0);
                p->pd_out = (uint8_t)jnum(pj, "pd_out", 0);
                p->vendor_id = (uint16_t)jnum(pj, "vendor_id", 0);
                p->device_id = (uint32_t)jnum(pj, "device_id", 0);

                char mn[24] = {0};
                jstr(pj, "mode", mn, sizeof mn);
                p->mode = mode_from_name(mn);
                n->port_count++;
            }
        }
    }
    cfg->node_count = nodes->n;

    /* 설정이 스스로 모순되면 여기서 멈춘다 — 버스에 나가 보고 알 일이 아니다 */
    if (cfg->image_in_bytes != 0u && cfg->image_in_bytes != in_off) {
        if (err) {
            snprintf(err, err_cap,
                     "process_image.in.bytes %u but the nodes add up to %u",
                     cfg->image_in_bytes, in_off);
        }
        jfree(root);
        return -1;
    }
    if (cfg->image_out_bytes != 0u && cfg->image_out_bytes != out_off) {
        if (err) {
            snprintf(err, err_cap,
                     "process_image.out.bytes %u but the nodes add up to %u",
                     cfg->image_out_bytes, out_off);
        }
        jfree(root);
        return -1;
    }
    if (cfg->image_in_bytes == 0u)  { cfg->image_in_bytes = in_off; }
    if (cfg->image_out_bytes == 0u) { cfg->image_out_bytes = out_off; }

    jfree(root);
    return 0;
}
