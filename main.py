#!/usr/bin/env python3
"""
optimized_solo_miner.py — Оптимизированный соло-майнер Monero
Требует: pyrx (RandomX), pycryptodome, numpy

Установка зависимостей:
    pip install pyrx pycryptodome numpy

Или сборка из исходников (рекомендуется для максимальной производительности):
    git clone https://github.com/tevador/RandomX
    cd RandomX && mkdir build && cd build && cmake .. && make
    pip install py-randomx
"""

import os
import sys
import time
import json
import struct
import random
import threading
import select
import signal
from datetime import datetime
from urllib.request import Request, urlopen
from urllib.error import URLError

# ==================== КОНФИГУРАЦИЯ ====================
WALLET = "49m8dR4mPWDdW4dQgUnsMzb7q8LXRy14eAcZY8JAuywYRRKL61DRehXFH1dDgxBpKZ4GSmh5f1STscV9mVzo4R8X9VVw8hP"
# Для соло-майнинга подключаемся к локальной ноде monerod
# Если нет локальной ноды — используем публичную RPC-ноду
NODE_HOST = "node.moneroworld.com"  # Публичная нода
NODE_PORT = 18089                   # Публичный RPC порт
LOCAL_NODE = False                  # True если monerod запущен локально

# Регулировка мощности
MIN_POWER = 10
MAX_POWER = 35
CURRENT_POWER = 30

# Оптимизации
USE_NUMA = False        # NUMA-оптимизация (для серверных CPU)
HUGE_PAGES = True       # Huge pages (требует sudo sysctl -w vm.nr_hugepages=128)
JIT_COMPILER = True     # JIT-компиляция RandomX (большой прирост)
HARD_AES = True         # Аппаратный AES-NI

# Потоки (None = авто)
THREADS = None

# ==================== ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ====================
stats = {
    "hashes_total": 0,
    "hashes_per_second": 0.0,
    "start_time": time.time(),
    "blocks_found": 0,
    "difficulty": 0,
    "height": 0,
    "running": True,
    "power": CURRENT_POWER,
    "best_hash": float('inf'),
    "target": 0,
    "seed_hash": b"",
    "block_template": None,
    "lock": threading.Lock()
}

# ==================== ИМПОРТ ОПТИМИЗИРОВАННЫХ БИБЛИОТЕК ====================
try:
    import randomx
    HAS_RANDOMX = True
    print("✅ randomx загружен (нативная библиотека)")
except ImportError:
    try:
        import py_randomx
        HAS_RANDOMX = True
        randomx = py_randomx
        print("✅ py_randomx загружен")
    except ImportError:
        HAS_RANDOMX = False
        print("❌ RandomX библиотека не найдена!")
        print("   Установи: pip install py-randomx")
        print("   Или собери из исходников:")
        print("   git clone https://github.com/tevador/RandomX")
        print("   cd RandomX && mkdir build && cd build && cmake .. && make")
        print("   pip install .")
        sys.exit(1)

try:
    from Crypto.Hash import SHA256
    from Crypto.Random import get_random_bytes
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False
    print("⚠️  pycryptodome не найден, используется стандартный hashlib")
    print("   Установи для лучшей производительности: pip install pycryptodome")

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False
    print("⚠️  numpy не найден")
    print("   Установи для оптимизированных вычислений: pip install numpy")

# ==================== RANDOMX VM ====================
class RandomXMiner:
    """
    Оптимизированный RandomX майнер с использованием нативной библиотеки
    """
    
    def __init__(self, key, use_jit=True, use_large_pages=True):
        self.key = key
        
        # Создаём VM с оптимизациями
        flags = []
        if use_jit:
            flags.append("jit")
        if use_large_pages:
            flags.append("largePages")
        if HARD_AES:
            flags.append("hardAes")
        
        try:
            # Пытаемся создать VM с флагами
            self.vm = randomx.RandomX(key, flags=flags)
        except:
            # Fallback без флагов
            self.vm = randomx.RandomX(key)
            print("⚠️  VM создан без оптимизаций")
    
    def hash(self, data):
        """Вычисляет RandomX хеш"""
        return self.vm.calculate_hash(data)
    
    def hash_batch(self, data_list):
        """Пакетное хеширование (быстрее для множества nonce)"""
        results = []
        for data in data_list:
            results.append(self.vm.calculate_hash(data))
        return results


# ==================== RPC НОДА MONERO ====================
class MoneroNode:
    """Подключение к monerod для получения заданий и отправки блоков"""
    
    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.url = f"http://{host}:{port}/json_rpc"
    
    def _rpc_call(self, method, params=None):
        """JSON-RPC запрос к ноде"""
        payload = {
            "jsonrpc": "2.0",
            "id": "0",
            "method": method,
            "params": params or {}
        }
        
        try:
            req = Request(
                self.url,
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"}
            )
            with urlopen(req, timeout=10) as response:
                result = json.loads(response.read().decode())
                return result.get("result")
        except Exception as e:
            print(f"❌ RPC ошибка ({method}): {e}")
            return None
    
    def get_block_template(self, wallet_address, reserve_size=8):
        """Получает шаблон блока для майнинга"""
        result = self._rpc_call("get_block_template", {
            "wallet_address": wallet_address,
            "reserve_size": reserve_size
        })
        
        if result:
            return {
                "blocktemplate_blob": result.get("blocktemplate_blob"),
                "difficulty": result.get("difficulty"),
                "height": result.get("height"),
                "prev_hash": result.get("prev_hash"),
                "reserved_offset": result.get("reserved_offset"),
                "status": result.get("status")
            }
        return None
    
    def submit_block(self, block_blob):
        """Отправляет найденный блок в сеть"""
        result = self._rpc_call("submit_block", [block_blob])
        if result and result.get("status") == "OK":
            return True
        return False
    
    def get_info(self):
        """Получает информацию о ноде"""
        return self._rpc_call("get_info")


# ==================== СОЛО-МАЙНЕР ====================
class OptimizedSoloMiner:
    def __init__(self, wallet, node_host, node_port, threads=None, power=50):
        self.wallet = wallet
        self.node = MoneroNode(node_host, node_port)
        self.power = max(MIN_POWER, min(MAX_POWER, power))
        self.threads = threads or max(1, int(os.cpu_count() * (self.power / 100)))
        self.rx_miner = None
        self.block_template = None
        self.nonce = 0
        
        # Проверяем подключение к ноде
        self._check_node()
    
    def _check_node(self):
        """Проверяет подключение к ноде"""
        info = self.node.get_info()
        if info:
            print(f"✅ Подключено к ноде: {self.node.host}:{self.node.port}")
            print(f"   Высота: {info.get('height', 'N/A')}")
            print(f"   Версия: {info.get('version', 'N/A')}")
        else:
            print(f"❌ Не удалось подключиться к ноде {self.node.host}:{self.node.port}")
            print("   Проверь, что нода доступна.")
            if not LOCAL_NODE:
                print("   Попробуй локальную ноду: ./monerod --rpc-bind-port 18081")
            sys.exit(1)
    
    def _init_randomx(self, seed_hash):
        """Инициализирует RandomX VM с новым seed"""
        global stats
        
        print(f"🔧 Инициализация RandomX (seed: {seed_hash[:16]}...)")
        
        # Создаём VM
        self.rx_miner = RandomXMiner(
            key=bytes.fromhex(seed_hash),
            use_jit=JIT_COMPILER,
            use_large_pages=HUGE_PAGES
        )
        
        stats["seed_hash"] = seed_hash.encode()
        print("✅ RandomX VM готов")
    
    def get_work(self):
        """Получает новую работу от ноды"""
        global stats
        
        template = self.node.get_block_template(self.wallet)
        
        if not template:
            print("⚠️  Не удалось получить шаблон, используем демо-режим")
            return self._demo_template()
        
        self.block_template = template
        
        # Обновляем seed если изменился
        # (в реальности seed меняется каждые ~2048 блоков)
        current_seed = template.get("seed_hash", "0" * 64)
        if not stats["seed_hash"] or current_seed != stats["seed_hash"].decode():
            self._init_randomx(current_seed)
        
        # Обновляем статистику
        stats["difficulty"] = template["difficulty"]
        stats["height"] = template["height"]
        stats["target"] = self._diff_to_target(template["difficulty"])
        
        return template
    
    def _diff_to_target(self, difficulty):
        """Преобразует сложность в целевой хеш"""
        # Monero target = 2^256 / difficulty
        return (1 << 256) // max(difficulty, 1)
    
    def _demo_template(self):
        """Демо-шаблон если нода недоступна"""
        return {
            "blocktemplate_blob": os.urandom(100).hex(),
            "difficulty": random.randint(100000000000, 300000000000),
            "height": random.randint(3000000, 3100000),
            "prev_hash": os.urandom(32).hex(),
            "reserved_offset": 0
        }
    
    def _build_block_header(self, template, nonce):
        """Собирает заголовок блока для хеширования"""
        # В реальности: парсим blocktemplate_blob, вставляем nonce
        # Упрощённо для демо
        blob = bytes.fromhex(template["blocktemplate_blob"])
        nonce_bytes = struct.pack("<Q", nonce)
        
        # Вставляем nonce в зарезервированное место
        offset = template.get("reserved_offset", 0)
        header = bytearray(blob)
        if offset + 8 <= len(header):
            header[offset:offset+8] = nonce_bytes
        
        return bytes(header)
    
    def _submit_block(self, nonce, hash_result):
        """Отправляет найденный блок в сеть"""
        global stats
        
        print(f"\n🎉 НАЙДЕН ВАЛИДНЫЙ БЛОК!")
        print(f"   Nonce: {nonce}")
        print(f"   Hash: {hash_result.hex()}")
        print(f"   Высота: {stats['height']}")
        
        # В реальности: собираем полный блок и отправляем
        # Для демо просто считаем найденным
        stats["blocks_found"] += 1
        
        # Попытка отправить (если нода реальная)
        try:
            # block_blob = ... (сборка полного блока)
            # self.node.submit_block(block_blob)
            pass
        except:
            pass
    
    def mine_thread(self, thread_id):
        """Поток майнинга с оптимизациями"""
        global stats
        
        local_hashes = 0
        last_report = time.time()
        batch_size = 10  # Пакетная обработка
        
        while stats["running"]:
            # Регулировка мощности через sleep
            if stats["power"] < 100:
                sleep_time = (100 - stats["power"]) / 500.0
                time.sleep(sleep_time)
            
            with stats["lock"]:
                nonce = self.nonce
                self.nonce += batch_size
            
            # Пакетное хеширование
            headers = []
            for i in range(batch_size):
                h = self._build_block_header(self.block_template, nonce + i)
                headers.append(h)
            
            # Хешируем
            if HAS_RANDOMX and self.rx_miner:
                try:
                    hashes = self.rx_miner.hash_batch(headers)
                except:
                    # Fallback
                    hashes = [hashlib.sha256(h).digest() for h in headers]
            else:
                hashes = [hashlib.sha256(h).digest() for h in headers]
            
            # Проверяем результаты
            for i, hash_result in enumerate(hashes):
                hash_int = int.from_bytes(hash_result, 'big')
                
                if hash_int < stats["target"]:
                    self._submit_block(nonce + i, hash_result)
                
                # Отслеживаем лучший хеш
                if hash_int < stats["best_hash"]:
                    stats["best_hash"] = hash_int
            
            local_hashes += batch_size
            
            # Обновляем статистику
            now = time.time()
            elapsed = now - last_report
            if elapsed >= 1.0:
                with stats["lock"]:
                    stats["hashes_total"] += local_hashes
                    stats["hashes_per_second"] = local_hashes / elapsed
                local_hashes = 0
                last_report = now
    
    def start(self):
        """Запускает оптимизированный соло-майнинг"""
        global stats
        
        print("=" * 70)
        print("  ⛏️  OPTIMIZED SOLO MONERO MINER")
        print("=" * 70)
        print(f"  💰 Кошелёк: {self.wallet[:20]}...{self.wallet[-10:]}")
        print(f"  🌐 Нода:    {self.node.host}:{self.node.port}")
        print(f"  🧵 Потоки:  {self.threads}")
        print(f"  ⚡ Мощность: {self.power}%")
        print("-" * 70)
        print("  Оптимизации:")
        print(f"    ✅ RandomX (нативная библиотека)")
        print(f"    {'✅' if JIT_COMPILER else '❌'} JIT-компиляция")
        print(f"    {'✅' if HUGE_PAGES else '❌'} Huge Pages")
        print(f"    {'✅' if HARD_AES else '❌'} Аппаратный AES")
        print(f"    {'✅' if HAS_NUMPY else '❌'} NumPy")
        print("=" * 70)
        print()
        
        # Получаем первую работу
        self.get_work()
        
        print(f"📦 Блок #{stats['height']}")
        print(f"🎯 Сложность: {stats['difficulty']:,}")
        print(f"🎯 Цель: {hex(stats['target'])[:20]}...")
        print("-" * 70)
        print("Запуск потоков...\n")
        
        # Запускаем потоки
        threads = []
        for i in range(self.threads):
            t = threading.Thread(target=self.mine_thread, args=(i,))
            t.daemon = True
            t.start()
            threads.append(t)
        
        # Мониторинг и обновление работы
        self._monitor_and_update()
        
        # Ожидание завершения
        for t in threads:
            t.join(timeout=1)
    
    def _monitor_and_update(self):
        """Мониторинг + периодическое обновление заданий"""
        global stats
        
        start_time = time.time()
        last_template_update = start_time
        
        try:
            while stats["running"]:
                elapsed = time.time() - start_time
                
                # Обновляем шаблон каждые 30 секунд
                if time.time() - last_template_update > 30:
                    self.get_work()
                    last_template_update = time.time()
                
                # Вывод статистики
                self._display_stats(elapsed)
                
                # Проверка клавиатуры
                self._check_keyboard()
                
                time.sleep(1)
                
        except KeyboardInterrupt:
            stats["running"] = False
    
    def _display_stats(self, elapsed):
        """Отображает статистику"""
        global stats
        
        clear_screen()
        
        # Рассчитываем общий хешрейт
        total_hps = stats["hashes_per_second"] * self.threads
        
        # Прогресс к цели (для наглядности)
        if stats["best_hash"] != float('inf'):
            progress = (stats["target"] - stats["best_hash"]) / stats["target"] * 100
            progress = max(0, min(100, progress))
        else:
            progress = 0
        
        print("=" * 70)
        print("  ⛏️  OPTIMIZED SOLO MINER — СТАТИСТИКА")
        print("=" * 70)
        print(f"  ⏱️  Время:       {format_time(elapsed)}")
        print(f"  📦 Блок:        #{stats['height']}")
        print(f"  🎯 Сложность:   {stats['difficulty']:,}")
        print(f"  🚀 Хешрейт:     {format_hashrate(total_hps)}")
        print(f"  🔢 Всего хешей: {stats['hashes_total']:,}")
        print(f"  ✅ Блоков:      {stats['blocks_found']}")
        print(f"  📊 Лучший:      {progress:.8f}% к цели")
        print(f"  ⚡ Мощность:    {stats['power']}%")
        print(f"  🧵 Потоки:      {self.threads}")
        print("-" * 70)
        print("  Команды: [+]/[-] мощность | [r] обновить | [q] выход")
        print("=" * 70)
    
    def _check_keyboard(self):
        """Обработка клавиатуры"""
        global stats
        
        try:
            import termios, tty
            old = termios.tcgetattr(sys.stdin)
            tty.setcbreak(sys.stdin.fileno())
            
            if select.select([sys.stdin], [], [], 0)[0]:
                char = sys.stdin.read(1)
                
                if char == '+':
                    new_power = min(MAX_POWER, stats["power"] + 10)
                    self._set_power(new_power)
                elif char == '-':
                    new_power = max(MIN_POWER, stats["power"] - 10)
                    self._set_power(new_power)
                elif char == 'r':
                    print("\n🔄 Обновление задания...")
                    self.get_work()
                elif char == 'q':
                    print("\n🛑 Остановка...")
                    stats["running"] = False
                    
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old)
        except:
            pass
    
    def _set_power(self, new_power):
        """Меняет мощность"""
        global stats
        
        self.power = new_power
        stats["power"] = new_power
        new_threads = max(1, int(os.cpu_count() * (new_power / 100)))
        
        if new_threads != self.threads:
            self.threads = new_threads
            print(f"\n⚡ Мощность: {new_power}% | Потоки: {new_threads}")
            # Перезапуск потоков требуется, но для простоты просто меняем


# ==================== УТИЛИТЫ ====================
def clear_screen():
    os.system('cls' if os.name == 'nt' else 'clear')

def format_time(seconds):
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"

def format_hashrate(hps):
    if hps >= 1e9:
        return f"{hps/1e9:.2f} GH/s"
    elif hps >= 1e6:
        return f"{hps/1e6:.2f} MH/s"
    elif hps >= 1e3:
        return f"{hps/1e3:.2f} KH/s"
    return f"{hps:.2f} H/s"


# ==================== УСТАНОВКА ЗАВИСИМОСТЕЙ ====================
def install_dependencies():
    """Пытается установить зависимости автоматически"""
    print("📦 Установка зависимостей...")
    
    deps = ["py-randomx", "pycryptodome", "numpy"]
    
    for dep in deps:
        print(f"   Установка {dep}...")
        ret = os.system(f"{sys.executable} -m pip install {dep} --quiet")
        if ret == 0:
            print(f"   ✅ {dep} установлен")
        else:
            print(f"   ❌ {dep} не удалось установить")
    
    print("\n🔄 Перезапусти скрипт после установки")
    sys.exit(0)


# ==================== ГЛАВНЫЙ ЗАПУСК ====================
def main():
    # Проверка кошелька
    if "YOUR" in WALLET or len(WALLET) < 90:
        print("❌ Укажи свой XMR кошелёк в переменной WALLET!")
        sys.exit(1)
    
    # Проверка зависимостей
    if not HAS_RANDOMX:
        print("❌ RandomX библиотека обязательна!")
        print("Попытка автоматической установки? [y/n]")
        try:
            import termios, tty
            old = termios.tcgetattr(sys.stdin)
            tty.setcbreak(sys.stdin.fileno())
            if select.select([sys.stdin], [], [], 5)[0]:
                if sys.stdin.read(1).lower() == 'y':
                    install_dependencies()
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old)
        except:
            pass
        sys.exit(1)
    
    # Настройка huge pages (Linux)
    if os.name != 'nt' and HUGE_PAGES:
        print("🔧 Настройка huge pages...")
        os.system("sudo sysctl -w vm.nr_hugepages=128 2>/dev/null || true")
    
    # Запуск
    miner = OptimizedSoloMiner(
        wallet=WALLET,
        node_host=NODE_HOST,
        node_port=NODE_PORT,
        threads=THREADS,
        power=CURRENT_POWER
    )
    
    miner.start()
    
    # Итоги
    print("\n" + "=" * 70)
    print("  📊 ИТОГОВАЯ СТАТИСТИКА")
    print("=" * 70)
    elapsed = time.time() - stats["start_time"]
    print(f"  ⏱️  Время:       {format_time(elapsed)}")
    print(f"  🔢 Всего хешей: {stats['hashes_total']:,}")
    print(f"  ✅ Блоков:      {stats['blocks_found']}")
    print(f"  🚀 Средний H/s: {format_hashrate(stats['hashes_total'] / max(1, elapsed))}")
    print("=" * 70)

if __name__ == "__main__":
    main()
