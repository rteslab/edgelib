# edgelib - build and install
#
# Copyright (c) 2026 RTES Co., Ltd. All rights reserved.
#
#   make            libedgelib.so and libedgelib.a
#   make install    into /usr/local  (install.sh calls this)
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
