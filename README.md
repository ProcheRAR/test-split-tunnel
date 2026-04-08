# Vopono GUI

Графическая обёртка для [vopono](https://github.com/jamesmcm/vopono) — утилиты, которая запускает приложения внутри VPN-туннеля через отдельный network namespace.

## Зависимости

### Обязательные

| Пакет | Описание |
|-------|----------|
| `python-gobject` | GTK3 биндинги для Python |
| `gtk3` | GTK 3 |
| `vopono` | CLI-утилита для VPN namespace |
| `wireguard-tools` | `wg`, `wg-quick` — инструменты WireGuard |

### Опциональные

| Пакет | Описание |
|-------|----------|
| `libayatana-appindicator` | Иконка в системном трее (рекомендуется) |
| `zenity` | Графический диалог ввода пароля (или `kdialog`, `ksshaskpass`) |

### Установка (Arch Linux)

```bash
# Обязательные
sudo pacman -S python-gobject gtk3 wireguard-tools

# vopono из AUR
yay -S vopono

# Опциональные
sudo pacman -S libayatana-appindicator zenity
```

## Запуск

```bash
python3 vopono_gui.py
```

### Через .desktop файл

Скопируйте `vopono-gui.desktop` в `~/.local/share/applications/` и отредактируйте путь в строке `Exec=`:

```bash
cp vopono-gui.desktop ~/.local/share/applications/
```

## Как работает

1. Вы выбираете `.conf` файл WireGuard и одно или несколько приложений
2. GUI создаёт временную копию конфига с исправленными полями (Address без CIDR маски, лишний ListenPort)
3. Формирует команду `vopono exec --custom <conf> --protocol wireguard "bash -c 'app1 & app2 & wait'"`
4. Для графического ввода пароля подменяет `sudo` обёрткой, которая вызывает настоящий `sudo -A` с `SUDO_ASKPASS` указывающим на zenity/kdialog/ksshaskpass
5. При закрытии корректно завершает процесс и очищает namespace

## Лицензия

MIT
