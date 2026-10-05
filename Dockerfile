# Собирается нативно и под arm64 (Apple Silicon), и под amd64 (VPS):
# у всех зависимостей есть готовые колёса, компилировать ничего не нужно.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONIOENCODING=utf-8 \
    WLPARSER_CONFIG=/app/config/config.yaml \
    WLPARSER_ENV=/app/config/.env

WORKDIR /app

# Без tzdata переменная TZ не действует, и контейнер живёт в UTC: время в
# логах разъезжается с местным, а расписание постинга уедет на разницу
# часовых поясов.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

# Метаданные и код отдельным слоем: пока pyproject.toml не менялся,
# пересборка не тянет зависимости заново.
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir .

# Объявлен только теперь, не раньше тяжёлого pip install выше: значение
# меняется на каждый коммит, и если объявить его раньше, каждая пересборка
# заново качала бы все зависимости вместо переиспользования слоя.
ARG GIT_COMMIT=unknown

# twikit — отдельно и всегда свежим, не из кеша слоя выше. Она публикует
# посты в X, обходя её защиту от ботов, а X эту защиту меняет часто (живой
# пример: ошибка "Couldn't get KEY_BYTE indices" в x_client_transaction —
# эту конкретную регрессию апстрим держит открытой без релиза уже полгода,
# поэтому она обойдена патчем в коде, см. poster/twitter_client.py, но
# следующую подобную поломку так ловить не придётся, если версия свежая).
# GIT_COMMIT тут не нужен самой библиотеке — он только взламывает кеш
# этого слоя, чтобы --upgrade реально проверял PyPI на каждой пересборке,
# а не повторял старый вывод.
RUN echo "cache-bust: ${GIT_COMMIT}" > /dev/null \
    && pip install --no-cache-dir --upgrade twikit

# Образец конфига лежит вне /app/config, иначе том его перекроет.
COPY config/config.example.yaml /app/config.example.yaml

# Виден в /menu → Статус — так владелец видит, что Мак реально обновился,
# не заходя по SSH.
ENV GIT_COMMIT=$GIT_COMMIT

RUN useradd --create-home --uid 1000 wlparser \
    && mkdir -p /app/data /app/config \
    && chown -R wlparser:wlparser /app
USER wlparser

# Настройки и база живут в томах и переживают пересборку образа.
VOLUME ["/app/data", "/app/config"]

ENTRYPOINT ["python", "-m", "wlparser"]
CMD ["run"]
