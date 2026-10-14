# Coding agent с tool calling

Консольный агент: получает задачу на изменение небольшого проекта, сам выбирает инструменты, читает и меняет файлы, запускает тесты и решает, когда закончить.

Всё ниже написано с нуля, без агентских фреймворков: agent loop, policy, восстановление после сбоя, бюджеты, Plan Mode, задачи с зависимостями, журнал изменений и откат.
Стек: Python 3.10+, stdlib + PyYAML. Модель подключается через любой OpenAI-compatible Chat Completions API. По умолчанию это [BasisWeb](https://api.basisweb.ru/v1) (модели GPT / Claude).

Полное ТЗ: [TZ.md](TZ.md).

## Содержание

1. [Требования](#требования)
2. [Быстрый старт](#быстрый-старт)
3. [Установка](#установка)
4. [Настройка модели](#настройка-модели)
5. [config.yaml — справочник](#configyaml--справочник)
6. [policy.yaml — справочник](#policyyaml--справочник)
7. [Make-команды](#make-команды)
8. [CLI-команды](#cli-команды)
9. [Сценарии запуска](#сценарии-запуска)
10. [Статусы и хранение состояния](#статусы-и-хранение-состояния)
11. [Инструменты агента](#инструменты-агента)
12. [Как это работает](#как-это-работает)
13. [Тесты](#тесты)
14. [Структура проекта](#структура-проекта)
15. [Решение проблем](#решение-проблем)

---

## Требования

- Linux / macOS / WSL (нужен `bash`; на Windows используйте WSL).
- Python 3.10+ (`python3 --version`).
- `make` (необязательно: все цели — обёртки над `python -m agent`).
- API-ключ BasisWeb.

## Быстрый старт

```bash
make venv              # .venv + PyYAML
make config            # создать config.yaml из config.example.yaml
# впишите ключ в config.yaml:  llm.api_key: "..." 
make models            # проверить ключ и посмотреть доступные модели
make test              # тесты runtime, модель не нужна
make run               # контрольная задача в режиме auto
make test-workspace    # тесты контрольного проекта после работы агента
```

Вернуть `workspace/order.py` в исходное (сломанное) состояние для повторного прогона можно двумя способами: `make rollback RUN=<id>` (через журнал) или `make reset`.

## Установка

С виртуальным окружением (рекомендуется):

```bash
make venv
# то же вручную:
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Если `.venv` существует, Makefile сам использует его интерпретатор. Иначе берётся `python3`.

Без venv:

```bash
make install                      # pip install -r requirements.txt в текущий python
```

Единственная зависимость — `PyYAML`. HTTP-клиент к модели написан на `urllib`.

## Настройка модели

### BasisWeb

1. Создайте `config.yaml` и впишите ключ в `llm.api_key`:

   ```bash
   make config                    # копирует config.example.yaml -> config.yaml
   ```

2. Проверьте ключ и выберите модель:

   ```bash
   make models                    # GET https://api.basisweb.ru/v1/models
   ```

3. Впишите выбранный id в `config.yaml` (по умолчанию `gpt-4o-mini`) или задайте переменной окружения `OPENAI_MODEL=<id>`. Модель должна поддерживать tool calling: GPT-4o / GPT-4.1 / Claude поддерживают.

`config.yaml` (создаётся из `config.example.yaml`, в `.gitignore`):

```yaml
llm:
  base_url: https://api.basisweb.ru/v1
  api_key: "ваш_ключ"     # config.yaml в .gitignore, в репозиторий не попадает
  model: gpt-4o-mini
  temperature: 0
  timeout: 300
```

Ключ хранится только в локальном `config.yaml` (в `.gitignore`); в `config.example.yaml` его не пишите. Если ключа нет, агент сразу выдаст ошибку `API key is empty`, ещё до запроса.

### Переменные окружения

Переменные окружения сильнее `config.yaml`.

| Переменная | Поле конфига |
|---|---|
| `OPENAI_BASE_URL` | `llm.base_url` |
| `OPENAI_API_KEY` | `llm.api_key` |
| `OPENAI_MODEL` | `llm.model` |

```bash
OPENAI_MODEL=<id из make models> make run    # разовая смена модели
```

Подойдёт любой другой OpenAI-compatible сервис: достаточно `base_url`, ключа и имени модели.

### Несколько конфигов

```bash
cp config.yaml config.local.yaml          # свои настройки
make run CONFIG=config.local.yaml
python -m agent --config config.local.yaml run "..."
```

## config.yaml — справочник

Относительные пути считаются от расположения самого `config.yaml`.

| Ключ | По умолчанию | Описание |
|---|---|---|
| `llm.base_url` | `https://api.basisweb.ru/v1` | Адрес OpenAI-compatible API |
| `llm.api_key` | `""` | Ключ API (только в локальном `config.yaml`) |
| `llm.model` | `gpt-4o-mini` | Id модели (`make models`) |
| `llm.temperature` | `0` | Температура генерации |
| `llm.timeout` | `300` | Таймаут одного запроса к модели, сек |
| `policy` | `policy.yaml` | Путь к policy. Файл обязан лежать вне workspace |
| `runs_dir` | `runs` | Где хранить состояние запусков, снимки и журнал |
| `mode` | `safe` | Режим по умолчанию: `safe` или `auto`. Флаг `--mode` его перекрывает |
| `require_verification` | `true` | Финальный ответ принимается, только если после последнего изменения файлов был успешный bash (тесты). Иначе модель получает напоминание |
| `max_finish_nudges` | `5` | Сколько раз возвращать модель к работе, прежде чем принять ответ как есть |
| `limits.bash_timeout` | `30` | Таймаут одной bash-команды, сек |
| `limits.max_output` | `8000` | Максимум символов в одном результате инструмента |
| `budget.model_requests` | `30` | Лимит запросов к модели на один run |
| `budget.tool_calls` | `60` | Лимит tool calls на один run |
| `budget.side_effects` | `15` | Лимит действий с побочным эффектом: запись, удаление, bash |
| `budget.output_chars` | `200000` | Суммарный размер результатов инструментов, вернувшихся модели |

Лимиты бюджета фиксируются в момент создания run. Изменение конфига на уже идущие запуски не влияет.

## policy.yaml — справочник

```yaml
workspace: workspace          # корень проекта агента, относительно policy.yaml

commands:
  allow:                      # ТОЧНЫЕ команды (пробелы нормализуются)
    - python3 -m unittest -v
    - python3 -m unittest
    - python3 -m unittest -v test_order.py
    - python3 -m unittest test_order.py
    - ls
    - ls -la
  deny:                       # префиксы токенов: "rm" запрещает "rm x", "rm -rf /" …
    - rm
    - git
    - curl
    - python3 -c
    - bash

directories:
  allow:                      # относительно workspace; канонизируются, обязаны быть внутри него
    - .
  deny:
    - private
```

Правила:

- **Deny сильнее allow.** Всё, что не разрешено явно, отклоняется.
- **Команды:** shell-синтаксис запрещён целиком: `| & ; < > ( ) $ \` \ " ' * ? [ ] { } ~ ! # =`, переводы строк. Поэтому конвейеры, редиректы, подстановки, `bash -c '...'`, `A=b cmd` не пройдут. Разрешается только простая команда из `allow`, буква в букву.
- **Директории:** относятся и к файловым инструментам, и к `cwd` для bash. Запрет директории распространяется на все её поддиректории.
- **Пути от модели:** абсолютные пути, `..` и пути через симлинк отклоняются.
- **Сам policy-файл** лежит вне workspace, и путь к нему дополнительно блокируется. Если policy окажется внутри workspace, агент не запустится.

Как добавить команду: впишите её точной строкой в `commands.allow`. Например, `python3 -m unittest -v test_order.py` уже добавлена: модели часто запускают тесты, указывая файл явно. Разрешённые команды попадают в системный промпт, так что модель их видит.

> Policy не заменяет изоляцию процесса: разрешённая программа работает с правами пользователя. Используйте учебную копию проекта без секретов.

## Make-команды

`make help` выводит список. Переменные передаются так: `make <цель> VAR=value`.

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `RUN` | — | run_id (обязателен для команд над существующим запуском) |
| `MODE` | `auto` | `safe` / `auto` для `run`, `run-plan` |
| `TASK` | контрольная задача | Текст задачи |
| `CONFIG` | `config.yaml` | Конфиг агента |
| `REASON` | — | Причина для `deny`, `plan-reject` |
| `CHOICE` | `retry` | `retry` / `fail` / `abort` для `resolve` |
| `ATTR` | — | `yes` / `no` для `resolve`: относить ли изменения после сбоя к run |
| `PYTHON` | `python3` | Интерпретатор, если нет `.venv` |

| Цель | Что делает |
|---|---|
| `make venv` / `make install` | Установка зависимостей |
| `make config` | Создать `config.yaml` из `config.example.yaml` |
| `make models` | Проверить ключ и показать доступные модели |
| `make run [MODE=] [TASK=]` | Новый запуск |
| `make run-safe` | Новый запуск в safe |
| `make run-plan` | Новый запуск в Plan Mode |
| `make list` | Все запуски |
| `make status RUN=` | Статус, бюджет и что ожидается от пользователя |
| `make resume RUN=` | Продолжить после перезапуска или сбоя |
| `make approve RUN=` / `make deny RUN= REASON=` | Подтвердить / отклонить действие |
| `make plan RUN=` | Показать план |
| `make plan-approve RUN=` / `make plan-reject RUN= REASON=` | Утвердить / отклонить план |
| `make resolve RUN= CHOICE= [ATTR=]` | Разрешить неопределённый вызов после сбоя |
| `make tasks RUN=` | Состояние задач |
| `make diff RUN=` | Изменения файлов запуска |
| `make rollback RUN=` | Откат изменений запуска |
| `make crash-demo` | Запуск с имитацией сбоя после первого побочного эффекта |
| `make test` | Тесты runtime |
| `make test-workspace` | Тесты контрольного проекта |
| `make reset` | Сбросить контрольную задачу: исходный (сломанный) `order.py` и нетронутый `test_order.py` |
| `make clean` / `make clean-runs` | Удалить кеши / удалить все сохранённые запуски |

## CLI-команды

```text
python -m agent [--config config.yaml] <команда>

run "<задача>" [--mode safe|auto] [--plan]   новый запуск, печатает run_id
list                                         все запуски
models                                       доступные модели API
status <run_id>                              статус, бюджет, ожидаемое действие
resume <run_id>                              продолжить
approve <run_id>                             подтвердить ожидающее действие
deny <run_id> [--reason "..."]               отклонить ожидающее действие
plan <run_id>                                показать план
plan-approve <run_id>                        утвердить план
plan-reject <run_id> [--reason "..."]        отклонить план
resolve <run_id> retry|fail|abort [--attribute yes|no]
tasks <run_id>                               задачи
diff <run_id>                                diff изменений запуска
rollback <run_id>                            откат изменений запуска
```

Команды-решения (`approve`, `deny`, `plan-approve`, `resolve`) сразу продолжают тот же запуск до следующей остановки.

**Интерактивный режим.** Если команда запущена из терминала (`run`, `resume` и другие), агент не выходит на каждой остановке, а спрашивает прямо в консоли: `approve? [y/n]`, `approve plan? [y/n]`, `retry/fail/abort?`. При ответе `n` можно указать причину, её получит модель. Цикл идёт, пока запуск не закончится (`done`, `failed`, `budget_exhausted`, `plan_rejected`). Ctrl+C или Ctrl+D на вопросе — пауза: состояние уже сохранено, продолжить можно через `resume <run_id>`. Без терминала (пайп, скрипт) агент, как раньше, останавливается и ждёт команд `approve` / `deny` / `plan-approve` / `resolve`.

Коды выхода: `0` — ок, `1` — ошибка пользователя или конфликт, `2` — модель недоступна (состояние сохранено, можно сделать `resume`), `3` — имитированный сбой.

## Сценарии запуска

### 1. Контрольная задача, режим auto

```bash
make run
# run_id: 20261013-130827-b8eda1
# [tool] list_files -> done ...
# [exec] write_file {"path": "order.py", ...}
# [exec] bash {"command": "python3 -m unittest -v"}
# status: done
make test-workspace
make diff RUN=20261013-130827-b8eda1
```

### 2. Режим safe: подтверждение действий

```bash
make run-safe
# [confirm] write_file {...}   status: awaiting_confirmation
# approve? [y/n] y                          # выполнить и продолжить
# approve? [y/n] n
# reason: не трогай тесты                   # модель получит отказ с причиной
```

Без терминала то же делается отдельными командами: `make approve RUN=<id>`, `make deny RUN=<id> REASON="..."`.

В safe подтверждения требуют запись и bash, в auto — только удаление. Чтение выполняется сразу в обоих режимах.

### 3. Plan Mode и задачи

```bash
make run-plan MODE=safe
# модель изучает проект (только чтение) и вызывает submit_plan
# status: awaiting_plan_approval  + JSON плана
make plan RUN=<id>
make plan-approve RUN=<id>                  # или plan-reject RUN=<id> REASON="..."
make tasks RUN=<id>
#     t1  done         deps=-   verify={'kind': 'file_read', 'target': 'order.py'}  ...
#     t2  in_progress  deps=t1  verify={'kind': 'file_changed', 'target': 'order.py'}
#     t3  pending      deps=t2  verify={'kind': 'command_succeeded', 'target': 'python3 -m unittest -v'}
```

### 4. Сбой и восстановление

```bash
make crash-demo                             # упадёт после первой записи/bash, до сохранения результата
make status RUN=<id>                        # status: awaiting_resolution, вызов uncertain
make resolve RUN=<id> CHOICE=retry ATTR=yes # повторить; изменения после сбоя отнести к запуску
make resolve RUN=<id> CHOICE=fail ATTR=no   # считать шаг неуспешным и продолжить
make resolve RUN=<id> CHOICE=abort          # завершить запуск с ошибкой
```

Сбой можно включить и вручную: `AGENT_FAULT=after_execute python -m agent run "..."`.

### 5. Просмотр изменений и откат

```bash
make diff RUN=<id>
make rollback RUN=<id>      # восстанавливает только файлы этого запуска
```

Если файл, изменённый агентом, потом поправили вручную, откат сообщит о конфликте и не тронет ни один файл.

### 6. Исчерпание бюджета

Уменьшите, например, `budget.model_requests` до `2` в своём конфиге и запустите `make run CONFIG=...`. Запуск остановится со статусом `budget_exhausted`, а `make status` покажет счётчики.

## Статусы и хранение состояния

**Статусы запуска**

| Статус | Значение | Что делать |
|---|---|---|
| `running` | Идёт работа, либо процесс упал во время запроса к модели | `resume` |
| `awaiting_confirmation` | Нужно подтверждение действия | `approve` / `deny` |
| `awaiting_plan_approval` | План готов | `plan-approve` / `plan-reject` |
| `awaiting_resolution` | После сбоя есть неопределённый вызов | `resolve` |
| `done` | Финальный ответ получен | `diff`, `rollback` |
| `budget_exhausted` | Исчерпан лимит | `status` |
| `plan_rejected` / `failed` | Завершён пользователем или с ошибкой | — |

**Статусы tool call:** `pending`, `awaiting_confirmation`, `started`, `uncertain`, `done`, `error`, `denied`, `failed`, `rejected` (бюджет).

**Файлы запуска** (`runs/<run_id>/`):

- `state.json` — всё состояние: сообщения, tool calls (имя, аргументы, статус, попытки, результат, `task_id`), решения пользователя, план, задачи, бюджет, журнал. Пишется атомарно (tmp + fsync + rename).
- `snapshots/<n>.json` — снимки содержимого workspace для diff и отката.

## Инструменты агента

| Инструмент | Тип | Подтверждение safe / auto | Повтор после сбоя |
|---|---|---|---|
| `list_files(path?)` | чтение | нет / нет | да |
| `read_file(path)` | чтение | нет / нет | да |
| `write_file(path, content)` | запись | да / нет | да: пишет полное содержимое, результат идемпотентен |
| `delete_file(path)` | удаление | да / да | да: повтор либо удаляет, либо возвращает «not found» |
| `bash(command, cwd?)` | bash | да / нет | **нет**: побочные эффекты произвольной программы неизвестны |
| `submit_plan(...)` | control | — (только Plan Mode) | да |
| `complete_task(task_id, summary?)` | control | — (только при наличии задач) | да |

- Каждый инструмент передаётся модели с именем, описанием и JSON Schema аргументов.
- Результат возвращается сообщением `role: tool` с исходным `tool_call_id`.
- Bash возвращает `stdout`, `stderr` и `exit_code`. Работает с таймаутом и ограничением вывода, запускается с `PYTHONDONTWRITEBYTECODE=1`, чтобы не плодить `__pycache__`.

## Как это работает

### Agent loop (`agent/runtime.py`)

1. Запрос к модели: бюджет списывается и сохраняется до запроса.
2. Если в ответе есть tool calls, вся пачка проходит проверку бюджета: либо выполняется целиком, либо не выполняется ничего.
3. Каждый вызов проходит проверки по порядку: валидный JSON → известный инструмент → ограничения фазы (Plan Mode) → `Policy.check` (схема, команда, пути, cwd, режим) → подтверждение пользователя → сверка журнала → бюджет побочных эффектов → исполнение.
4. Ошибка инструмента не роняет процесс: модель получает понятный результат и выбирает следующий шаг.
5. Ответ без tool calls завершает запуск. Исключения, когда модель получает напоминание: Plan Mode, незакрытые задачи, а при `require_verification` — нет успешного bash после последнего изменения файлов (не больше `max_finish_nudges` раз). Так слабая модель не завершит работу, вставив код в текст вместо `write_file`.

### Безопасность (`agent/policy.py`)

Policy отделена от loop и проверяется заново перед каждым исполнением: после подтверждения, после resume, при retry.

### Восстановление

- Перед побочным эффектом в одной атомарной записи сохраняются `status=started`, `attempts+1` и списание `side_effects`. После выполнения сохраняются результат, diff и списание `output_chars`.
- Если при загрузке найден вызов в статусе `started`, он помечается как `uncertain` и молча не повторяется.
- Если повторный `tool_call_id` совпадает с сохранённым по имени и аргументам, возвращается сохранённый результат. Если отличается — `protocol_error`.

### Бюджет

- Счётчики хранятся в `state.json`, поэтому `resume`, утверждение плана и retry их не обнуляют.
- Инструмент получает `min(max_output, остаток)`. Если результат всё равно не влезает, runtime обрезает его и помечает: `{"truncated": true, "partial": ...}`.

### Задачи

- Текущая задача — первая незавершённая, у которой все зависимости `done`.
- Tool calls фазы исполнения получают `task_id`, вызовы планирования — нет.
- `complete_task` проходит, только если runtime находит в этой задаче успешный вызов по правилу `verify`:
  - `command_succeeded` — bash с нужной командой и `exit_code 0`;
  - `file_changed` — журнал зафиксировал изменение файла;
  - `file_read` — файл прочитан;
  - `any_tool` — любой успешный вызов.

### Журнал и откат (`agent/journal.py`)

- При старте сохраняется снимок workspace.
- Перед каждым побочным эффектом дерево сверяется с последним снимком. Если что-то изменено извне, возвращается `workspace_conflict`.
- После действия сохраняются новый снимок, unified diff и sha256. Для каждого файла хранятся исходное и итоговое состояния.
- Откат идёт по принципу «всё или ничего»: заново проверяются policy, пути и хеши, и при любом расхождении не пишется ничего.
- Пока есть `uncertain` вызов, откат запрещён.
- Генерируемые файлы исключены из журнала: `__pycache__/`, `.pytest_cache/`, `*.pyc`. Симлинки тоже не учитываются.

## Тесты

```bash
make test
# или
python3 -m unittest discover -s tests -v
```

Модель в тестах заменена скриптом, поэтому прогон детерминированный, а ключ не нужен. Что покрыто:

- **Loop:** контрольная задача (исправление + проверка через bash, `test_order.py` не изменён, несколько вызовов в ответе, `role: tool` с id); ошибки инструментов.
- **Policy:** разрешённая и запрещённая команды, shell-синтаксис, разрешённая команда в запрещённой директории, запрещённая поддиректория для файлового инструмента, `..`, абсолютный путь, симлинк, policy внутри workspace; safe/auto, approve/deny после перезапуска.
- **Восстановление:** сбой между исполнением и сохранением; retry / fail / abort; bash не повторяется; повтор `tool_call_id`; `protocol_error`.
- **Plan Mode:** нет побочных эффектов до утверждения; отклонение плана; невалидные зависимости.
- **Задачи:** 3 задачи с зависимостями; `complete_task` без доказательства отклоняется; после resume завершённые задачи не перезапускаются.
- **Бюджет:** исчерпание каждого лимита; пачка «всё или ничего»; обрезка вывода; счётчики после resume; бюджет не возвращается после сбоя.
- **Журнал:** diff; откат после перезапуска; конфликт при посторонней правке; блокировка действия при внешнем изменении.

## Структура проекта

```text
.
├── Makefile
├── README.md
├── TZ.md                  # техническое задание
├── config.example.yaml    # шаблон config.yaml: модель, режим, лимиты, бюджет
├── config.yaml            # локальный конфиг (из шаблона, в .gitignore)
├── policy.yaml            # policy (вне workspace)
├── requirements.txt
├── agent/
│   ├── __main__.py        # CLI
│   ├── runtime.py         # agent loop, Plan Mode, задачи, бюджет, решения пользователя
│   ├── policy.py          # проверки команд, путей, режимов
│   ├── tools.py           # реестр инструментов, JSON Schema, executor
│   ├── journal.py         # снимки, diff, откат
│   ├── store.py           # атомарное хранилище состояния
│   ├── llm.py             # OpenAI-compatible клиент
│   └── config.py          # загрузка config.yaml + env
├── tests/test_agent.py
└── workspace/             # контрольный проект
    ├── order.py
    └── test_order.py
```

## Решение проблем

| Симптом | Причина / решение |
|---|---|
| `FileNotFoundError: ... config.yaml` | Нет локального конфига: `make config` |
| `model error: API key is empty` | Нет ключа: впишите `llm.api_key` в `config.yaml` |
| `invalid_api_key` / `Неверный API-ключ BasisWeb` | Ключ в `config.yaml` неверный или с лишними пробелами |
| `model ... not found` / 404 от BasisWeb | Нет такой модели: `make models` и поправьте `llm.model` |
| Запрос к модели висит долго | Первый запрос загружает модель в память. Увеличьте `llm.timeout` |
| Модель отвечает текстом и не исправляет код | Включите `require_verification: true`. Если не помогает, возьмите модель крупнее или запускайте с `--plan` |
| `denied_by_policy: command not in allow list` | Модель придумала команду. Добавьте её в `commands.allow`, если она безопасна |
| `workspace_conflict` | Файлы workspace изменены вручную во время запуска. Верните их или начните новый запуск |
| `rollback aborted, nothing was written` | Файл после агента правили вручную. Верните правку или откатите вручную |
| `resolve the uncertain tool call first` | После сбоя сначала нужен `make resolve RUN=<id> CHOICE=...` |
| `policy file must live outside the workspace` | Поправьте `workspace:` в `policy.yaml` |
