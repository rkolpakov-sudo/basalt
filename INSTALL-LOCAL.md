# Установка этой сборки на другой компьютер

Этот форк — официальный Basalt **v0.5.0** плюс локальные доработки веб-интерфейса.
Ветка по умолчанию `local` и есть рабочая сборка: склонировал — уже на ней.

Ничего из upstream не удалено и не переписано, поэтому обновления тянутся обычным
`git merge` (см. [Обновление](#обновление-с-upstream)).

## Что в git, а что нет

| В репозитории | Вне репозитория |
|---|---|
| `serve/` — сервер и весь веб-интерфейс (включая доработки) | `bin/` — движок и библиотеки, ~800 МиБ |
| `tools/` — то, что импортирует сервер | `models/` — паки моделей, ~94 ГиБ |
| `docs/` | `.venv/` — Python-окружение |
| `data/` — профили экспертов и фикстуры бенча (16 файлов) | |
| `basalt_config.json`, `Basalt.bat`, `serve_ctl.ps1` | |

Артефакты в `.gitignore`: веса и бинарники в git не попадают.

## Требования

- Windows 10 1903+ x64, процессор с AVX2.
- NVIDIA Blackwell (sm_120) — RTX 50-й серии. Референсная машина: RTX 5090 Laptop, 24 ГБ VRAM.
- Драйвер ветки R580 или новее (CUDA 13).
- CUDA 13 runtime: `cudart64_13`, `cublas64_13`, `cublasLt64_13`, `nvblas64_13`.
- Python 3.10+.
- **32 ГБ RAM минимум.** Flash-Next держит в RAM ~12.6 ГиБ экспертов при 23.4 ГиБ полного
  набора; на 16 ГБ модель поедет, но будет постоянно читать SSD.
- ~120 ГБ свободного места (94 ГиБ модели + 0.8 ГиБ движок + запас).

## Шаг 1. Код

```powershell
git clone https://github.com/rkolpakov-sudo/basalt.git C:\basalt-local
cd C:\basalt-local
```

Ветка `local` — по умолчанию, переключаться никуда не нужно.

Репозиторий содержит историю upstream целиком (включая C++ исходники), но рабочее
дерево можно сузить до того, что нужно серверу:

```powershell
git sparse-checkout init --cone
git sparse-checkout set serve tools docs
```

Это необязательно: лишние `src/`, `include/`, `CMakeLists.txt` просто лежат на диске.

## Шаг 2. Движок `bin/`

Нужны 4 `.exe` и 9 `.dll`:

```
basalt.exe  basalt-bench.exe  basalt-probe.exe  basalt-vision.exe
cudart64_13.dll  cublas64_13.dll  cublasLt64_13.dll  nvblas64_13.dll
avcodec-63.dll  avformat-63.dll  avutil-61.dll  swresample-7.dll  swscale-10.dll
```

**Вариант A — скопировать с рабочей машины (проще и надёжнее).** Всё гарантированно
совпадёт по версиям:

```powershell
robocopy \\ИМЯ_СТАРОГО_ПК\C$\basalt-v0.4.1-windows-x64\bin .\bin /E
```

**Вариант B — собрать из официальных источников.**

1. `.exe` — из архива релиза
   [v0.5.0 windows-x64](https://github.com/jesdga95/basalt/releases/tag/v0.5.0)
   (в архиве лежат только `.exe`, библиотек там нет).
2. CUDA 13 runtime — из установщика CUDA 13.0 (лягут в `PATH`) либо скопировать
   четыре `*_13.dll` из `bin/` рабочей машины.
3. FFmpeg 9 shared — сборка с [gyan.dev](https://www.gyan.dev/ffmpeg/builds/) или
   [BtbN](https://github.com/BtbN/FFmpeg-Builds/releases); нужны файлы с суффиксом
   `-63` (`avcodec-63`, `avformat-63`), `avutil-61`, `swresample-7`, `swscale-10`.
   Положить рядом с `basalt.exe`.

FFmpeg нужен только для `"vision": "dynamic"` в конфиге (картинки и видео). Если
мультимодальность не нужна, уберите ключ `vision` у моделей — и FFmpeg не понадобится.

## Шаг 3. Python-окружение

`bin\basalt.exe serve` сам подхватывает `.venv` в корне, если он там есть.

```powershell
py -3 -m venv .venv
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\pip install huggingface_hub
```

`huggingface_hub` не входит в `requirements.txt` — он нужен только для скачивания
моделей на шаге 4.

## Шаг 4. Модели (~94 ГиБ)

Имена файлов должны совпадать с `basalt_config.json` до буквы.

```powershell
.venv\Scripts\hf download jesdga/Qwen3.8-Flash-Next-Basalt Qwen3.8-FN-UD-IQ2_XS.basalt --local-dir models
.venv\Scripts\hf download jesdga/Qwen3.8-27B-Basalt Qwen3.8-27B-QUASAR-NVFP4.basalt --local-dir models
```

Если `.venv\Scripts\hf` нет (старая версия `huggingface_hub`), тот же вызов называется
`huggingface-cli download`.

Ожидаемый результат:

```
models\Qwen3.8-FN-UD-IQ2_XS.basalt        74.37 GiB
models\Qwen3.8-27B-QUASAR-NVFP4.basalt    19.70 GiB
```

`UD-IQ2_XS` — самая компактная из приемлемых квантизаций Flash-Next (KLD 0.1829,
top-1 86.21 %). Менять её на `UD-IQ1_*` ради размера не стоит: качество просядет.

## Шаг 5. `data/` — уже в клоне

`data\profiles\` (4 профиля экспертов, включая `qwen4exp-48x512.prof` для Flash-Next)
и `data\bench\` (фикстуры для `basalt-bench` и `basalt-probe`) отслеживаются upstream
и приходят с клонированием — из архива релиза их копировать не нужно.

Свои профили, сгенерированные `--expert-profile-save`, в git не попадают: строка
`data/` в `.gitignore` скрывает только новые файлы, уже отслеживаемые остаются в репозитории.

## Шаг 6. Запуск

```powershell
.\Basalt.bat
```

или

```powershell
.\serve_ctl.ps1 start -Open
```

Сервер поднимется на `http://127.0.0.1:8080/`, первым грузится 27B (~18 с).
Переключение на Flash-Next:

```powershell
Invoke-RestMethod http://127.0.0.1:8080/v1/load -Method Post `
  -ContentType 'application/json' -Body '{"model":"Flash-Next"}'
```

Управление: `.\Basalt.bat stop|restart|status`.

## Шаг 7. Проверка установки

```powershell
(Invoke-RestMethod http://127.0.0.1:8080/v1/models).data.id
(Invoke-RestMethod http://127.0.0.1:8080/v1/status) | Select-Object model, loaded, cache_max_tokens
```

Ожидается: обе модели в списке, `loaded=True`, `cache_max_tokens=131072`.

Доработки веб-интерфейса живы, если отвечают эти адреса:

```powershell
Invoke-WebRequest http://127.0.0.1:8080/api/config      -UseBasicParsing | Select-Object StatusCode
Invoke-WebRequest http://127.0.0.1:8080/api/model/caps  -UseBasicParsing | Select-Object StatusCode
```

Статика в v0.5.0 живёт по `/web/<файл>`: `chat.js`, `settings.js`, `extra.js`,
`extra.css` должны отдавать 200.

## Шаг 8. OpenCode

Блок провайдера в `~/.config/opencode/opencode.jsonc` (в `providers`):

```jsonc
"basalt": {
  "name": "Basalt (Local)",
  "npm": "@ai-sdk/openai-compatible",
  "options": { "baseURL": "http://127.0.0.1:8080/v1", "apiKey": "basalt" },
  "models": {
    "Qwen3.8-27B QUASAR NVFP4": {
      "name": "Basalt: Qwen 27B NVFP4 (local, ctx 131k)",
      "toolCall": true,
      "reasoning": true,
      "interleaved": { "field": "reasoning_content" },
      "limit": { "context": 131072, "output": 8192 }
    },
    "Flash-Next": {
      "name": "Basalt: Qwen 3.8 Flash-Next (MoE, local, ctx 131k)",
      "toolCall": true,
      "reasoning": true,
      "interleaved": { "field": "reasoning_content" },
      "limit": { "context": 131072, "output": 8192 }
    }
  }
}
```

## Конфиг

`basalt_config.json` в репозитории — рабочая версия:

- 27B: `gpu 0`, `max-context 131072`, `kv int8`, `kv-resident 32768`, `vision dynamic`;
- Flash-Next: `gpu 0`, `max-context 131072`, `kv int8`, `vision dynamic`, `resident-experts true`;
- `engine.vram-reserve-mib: 700`.

Что менять на другой карте:

- **Меньше 24 ГБ VRAM** — Flash-Next (44.3 ГиБ в VRAM+RAM) начнёт читать SSD сильнее;
  `vram-reserve-mib` снижать нельзя (ниже 256 движок отказывается на Windows).
- **Больше VRAM** — можно поднять `vram-reserve-mib` и/или `kv-resident`.
- **Другая карта в системе** — поправить `gpu`.

`vram-reserve-mib: 256` проверялся на референсной машине: на трёх прогонах выигрыш
выглядел как −9…−12 %, но на семи прогонах разброс съел эффект (−6 %, Welch t = −1.9).
Оставлено 700 как воспроизводимая база. Менять — только с замерами.

## Обновление с upstream

```powershell
git fetch upstream --tags
git merge v0.6.0
```

Единственный ожидаемый конфликт — `.gitignore` (локальный блок правил для артефактов
установки против upstream-версии): оставить оба блока, затем `git add .gitignore`.

`serve/handler.py` git сливает сам — проверено на реальном merge v0.5.0: 285 файлов
обновились автоматически, конфликтов в коде нет.

**Обязательный второй шаг:** `bin/` не в git, а `serve/` и движок должны быть одной
версии. Заменить `.exe` в `bin/` из архива нового релиза (DLL не трогать), затем
`.\serve_ctl.ps1 restart`.

## Откат

```powershell
git reset --hard local-0.5.0    # или local-0.4.1
```

Метки `local-0.4.1` и `local-0.5.0` — зафиксированные рабочие состояния. Откат кода
требует и соответствующего `bin/` из архива той же версии.

## Что изменено относительно upstream

| Файл | Что |
|---|---|
| `serve/web/index.html` | изменён: вкладки «Монитор» и «Настройки», подключение своих скриптов |
| `serve/handler.py` | изменён: маршруты `/api/config` (GET и POST), `/api/model/caps`, `/api/vram`, `/api/restart` |
| `serve/api/config_api.py` | добавлен: чтение/запись конфига, runtime-статус |
| `serve/web/chat.js` | добавлен: чат с потоковой выдачей, reasoning, tool-calls |
| `serve/web/settings.js` | добавлен: редактор конфига в веб-интерфейсе |
| `serve/web/extra.js`, `serve/web/extra.css` | добавлены: обвязка вкладок |
| `basalt_config.json` | добавлен: две модели, ctx 131072 |
| `serve_ctl.ps1` | добавлен: start/stop/restart/toggle/status |
| `Basalt.bat` | добавлен: лаунчер |
