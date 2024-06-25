FROM python:3.12-bookworm

RUN pip3 install poetry
RUN poetry config virtualenvs.create false

WORKDIR /srv
COPY pyproject.toml /srv/pyproject.toml
COPY poetry.lock /srv/poetry.lock
RUN poetry install --no-root

COPY . /app
WORKDIR /app


CMD ["python3", "main.py"]
