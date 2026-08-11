.PHONY: install dev test lint index probar docker up down logs

install:
	pip install -r requirements.txt

dev:
	uvicorn app.main:app --reload --port 8000

test:
	pytest

index:
	python -m scripts.indexar_fichas

probar:
	python -m scripts.probar_conversacion --limpiar

docker:
	docker build -t pastoriza-bot .

up:
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f bot
