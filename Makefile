# edgelib — 빌드와 설치
#
# Copyright (c) 2026 RTES Co., Ltd. All rights reserved.
#
#   make            libedgelib.so 와 libedgelib.a
#   make install    /usr/local 로  (install.sh 가 이것을 부른다)
#   make clean

CC      ?= cc
PREFIX  ?= /usr/local

CFLAGS  ?= -O2 -g
CFLAGS  += -std=c99 -Wall -Wextra -Wconversion -Wsign-conversion \
           -Wshadow -Wpointer-arith -fPIC -pthread -D_GNU_SOURCE
LDFLAGS += -pthread

SRC     := lib/frame.c lib/transport.c lib/config.c lib/cycle.c lib/edgelib.c
OBJ     := $(SRC:.c=.o)
HDR     := include/edgelib.h
# 헤더 전부에 의존시킨다. 공개 헤더만 걸면 lib/*.h 를 고쳤을 때 일부 .o 만 다시
# 컴파일되어 번역 단위마다 구조체 배치가 달라진 채로 링크된다.
DEPS    := $(HDR) $(wildcard lib/*.h)

SO      := libedgelib.so
AR_LIB  := libedgelib.a

.PHONY: all clean install uninstall

all: $(SO) $(AR_LIB)

$(SO): $(OBJ)
	$(CC) -shared -o $@ $^ $(LDFLAGS)

$(AR_LIB): $(OBJ)
	$(AR) rcs $@ $^

%.o: %.c $(DEPS)
	$(CC) $(CFLAGS) -c $< -o $@

install: all
	install -d $(DESTDIR)$(PREFIX)/include $(DESTDIR)$(PREFIX)/lib
	install -m 644 $(HDR) $(DESTDIR)$(PREFIX)/include/
	install -m 755 $(SO)  $(DESTDIR)$(PREFIX)/lib/
	install -m 644 $(AR_LIB) $(DESTDIR)$(PREFIX)/lib/
	-ldconfig

uninstall:
	rm -f $(DESTDIR)$(PREFIX)/include/edgelib.h
	rm -f $(DESTDIR)$(PREFIX)/lib/$(SO) $(DESTDIR)$(PREFIX)/lib/$(AR_LIB)
	-ldconfig

clean:
	rm -f $(OBJ) $(SO) $(AR_LIB)
