import socket
import struct
import threading
import time
import ipaddress
import sys
import json

# Сетевые настройки по умолчанию
MULTICAST_GROUP = '239.255.42.99'
MULTICAST_PORT = 5007
BROADCAST_PORT = 5008
DISCOVERY_PORT = 5009

class P2PChat:
    def __init__(self):
        self.local_ip, self.netmask, self.broadcast_ip = self.get_network_info()
        self.username = input("Введите ваше имя: ").strip() or "Anonym"
        
        self.active_peers = {}  
        self.ignored_ips = set() 
        
        self.in_multicast_group = False
        self.running = True

        print(f"\n[Сеть] IP: {self.local_ip} | Маска: {self.netmask} | Broadcast: {self.broadcast_ip}")

        # 1. Сокет для Broadcast
        self.broadcast_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.broadcast_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.broadcast_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self.broadcast_sock.bind(('', BROADCAST_PORT))

        # 2. Сокет для Multicast (На Windows критично использовать порт и '' для bind)
        self.multicast_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        self.multicast_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        
        # Разрешаем многократное использование порта (важно для запуска нескольких окон на одном ПК)
        if hasattr(socket, 'SO_REUSEPORT'):
            try:
                self.multicast_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except Exception:
                pass

        self.multicast_sock.bind(('', MULTICAST_PORT))
        self.join_multicast()

        # 3. Сокет для поиска (Discovery)
        self.discovery_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.discovery_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.discovery_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self.discovery_sock.bind(('', DISCOVERY_PORT))

    def get_network_info(self):
        """Автоматическое определение IP, маски и broadcast-адреса"""
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(('8.8.8.8', 80))
            local_ip = s.getsockname()[0]
        except Exception:
            local_ip = '127.0.0.1'
        finally:
            s.close()
        
        # Вычисляем сеть и маску (предполагается стандартная /24 для локальных сетей дома/универа)
        try:
            network = ipaddress.ip_network(f"{local_ip}/24", strict=False)
            return local_ip, str(network.netmask), str(network.broadcast_address)
        except Exception:
            return local_ip, '255.255.255.0', '255.255.255.255'

    def join_multicast(self):
        """Присоединение к многоадресной группе (с пересозданием для Windows)"""
        try:
            # Если старый сокет существовал, закрываем его
            if hasattr(self, 'multicast_sock') and self.multicast_sock:
                try:
                    self.multicast_sock.close()
                except Exception:
                    pass

            # Создаем новый сокет заново (обходит баг Windows 10022)
            self.multicast_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            self.multicast_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            
            if hasattr(socket, 'SO_REUSEPORT'):
                try:
                    self.multicast_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                except Exception:
                    pass

            self.multicast_sock.bind(('', MULTICAST_PORT))

            # Подписываемся на группу
            mreq = socket.inet_aton(MULTICAST_GROUP) + socket.inet_aton('0.0.0.0')
            self.multicast_sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            self.multicast_sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
            
            self.in_multicast_group = True
            print("[Multicast] Вы успешно вошли в группу.")
        except Exception as e:
            print(f"[Ошибка] Не удалось войти в multicast группу: {e}")

    def leave_multicast(self):
        """Выход из многоадресной группы"""
        try:
            if hasattr(self, 'multicast_sock') and self.multicast_sock:
                mreq = socket.inet_aton(MULTICAST_GROUP) + socket.inet_aton('0.0.0.0')
                self.multicast_sock.setsockopt(socket.IPPROTO_IP, socket.IP_DROP_MEMBERSHIP, mreq)
                self.multicast_sock.close()
                self.multicast_sock = None
                
            self.in_multicast_group = False
            print("[Multicast] Вы вышли из группы. Сообщения Multicast больше не принимаются.")
        except Exception as e:
            print(f"[Ошибка] Ошибка выхода из группы: {e}")

    def start_listeners(self):
        """Запуск фоновых потоков для приема данных"""
        threading.Thread(target=self.listen_broadcast, daemon=True).start()
        threading.Thread(target=self.listen_multicast, daemon=True).start()
        threading.Thread(target=self.listen_discovery, daemon=True).start()
        threading.Thread(target=self.send_heartbeat, daemon=True).start()
        threading.Thread(target=self.clean_peers, daemon=True).start()

    def listen_broadcast(self):
        while self.running:
            try:
                data, addr = self.broadcast_sock.recvfrom(1024)
                if addr[0] == self.local_ip:
                    continue
                if addr[0] in self.ignored_ips:
                    continue
                message = json.loads(data.decode('utf-8'))
                print(f"\n[BROADCAST] {message['user']} ({addr[0]}): {message['text']}")
            except Exception:
                break

    def listen_multicast(self):
        while self.running:
            try:
                if not self.multicast_sock:
                    time.sleep(0.5)
                    continue
                data, addr = self.multicast_sock.recvfrom(1024)
                if addr[0] == self.local_ip:
                    continue
                if addr[0] in self.ignored_ips:
                    continue
                message = json.loads(data.decode('utf-8'))
                print(f"\n[MULTICAST] {message['user']} ({addr[0]}): {message['text']}")
            except Exception:
                time.sleep(0.5)  # Не падаем, а ждем пересоздания сокета

    def listen_discovery(self):
        """Прием сигналов от других приложений для составления списка IP"""
        while self.running:
            try:
                data, addr = self.discovery_sock.recvfrom(1024)
                if addr[0] != self.local_ip:
                    self.active_peers[addr[0]] = time.time()
            except Exception:
                break

    def send_heartbeat(self):
        """Периодическая рассылка сигналов присутствия"""
        while self.running:
            try:
                self.discovery_sock.sendto(b"PING", (self.broadcast_ip, DISCOVERY_PORT))
            except Exception:
                pass
            time.sleep(3)

    def clean_peers(self):
        """Удаление неактивных хостов из списка"""
        while self.running:
            now = time.time()
            inactive = [ip for ip, t in self.active_peers.items() if now - t > 10]
            for ip in inactive:
                del self.active_peers[ip]
            time.sleep(2)

    def send_message(self, text, mode='multicast'):
        payload = json.dumps({"user": self.username, "text": text}).encode('utf-8')
        if mode == 'broadcast':
            self.broadcast_sock.sendto(payload, (self.broadcast_ip, BROADCAST_PORT))
            print(f"[Я -> Broadcast]: {text}")
        elif mode == 'multicast':
            if not self.in_multicast_group or not self.multicast_sock:
                print("[Ошибка] Вы не состоите в multicast группе! Используйте /join")
                return
            
            temp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            temp_sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            
            # ВАЖНО: Принудительно указываем ОС, с какого именно IP-адреса отправлять мультикаст
            try:
                temp_sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(self.local_ip))
            except Exception as e:
                print(f"[Предупреждение] Не удалось задать интерфейс для мультикаста: {e}")

            temp_sock.sendto(payload, (MULTICAST_GROUP, MULTICAST_PORT))
            temp_sock.close()
            print(f"[Я -> Multicast]: {text}")

    def print_help(self):
        print("\n--- Доступные команды ---")
        print(" /b <текст>   - отправить широковещательное сообщение (Broadcast)")
        print(" /m <текст>   - отправить многоадресное сообщение (Multicast)")
        print(" /users       - показать список активных IP-адресов в сети")
        print(" /leave       - выйти из multicast группы")
        print(" /join        - войти в multicast группу")
        print(" /ignore <IP> - добавить хост в черный список")
        print(" /unignore <IP>- убрать хост из черного списка")
        print(" /help        - показать это меню")
        print(" /exit        - выход из программы\n")

    def run(self):
        self.start_listeners()
        self.print_help()

        while self.running:
            try:
                line = input().strip()
                if not line:
                    continue
                
                if line.startswith('/b '):
                    self.send_message(line[3:], 'broadcast')
                elif line.startswith('/m '):
                    self.send_message(line[3:], 'multicast')
                elif line == '/users':
                    print("\n--- Активные приложения (IP) ---")
                    if not self.active_peers:
                        print("Пока никого нет в сети.")
                    else:
                        for ip in self.active_peers:
                            ignored = " [ИГНОРИРУЕТСЯ]" if ip in self.ignored_ips else ""
                            print(f" - {ip}{ignored}")
                    print("-" * 31)
                elif line == '/leave':
                    self.leave_multicast()
                elif line == '/join':
                    self.join_multicast()
                elif line.startswith('/ignore '):
                    target_ip = line.split()[1]
                    self.ignored_ips.add(target_ip)
                    print(f"[Игнор] Хост {target_ip} добавлен в игнор-лист.")
                elif line.startswith('/unignore '):
                    target_ip = line.split()[1]
                    if target_ip in self.ignored_ips:
                        self.ignored_ips.remove(target_ip)
                        print(f"[Игнор] Хост {target_ip} удален из игнор-листа.")
                elif line == '/help':
                    self.print_help()
                elif line == '/exit':
                    self.running = True
                    break
                else:
                    # По умолчанию отправляем в multicast, если не указано иное
                    self.send_message(line, 'multicast')
            except KeyboardInterrupt:
                break

        print("Выход...")
        sys.exit(0)

if __name__ == '__main__':
    chat = P2PChat()
    chat.run()
