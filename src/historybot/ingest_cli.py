from __future__ import annotations

import argparse
import asyncio
import json

from .ingestion import IngestionService
from .store import KnowledgeStore


async def _run(args: argparse.Namespace) -> None:
    store = KnowledgeStore(args.db)
    service = IngestionService(store)
    if args.command == "submit":
        options = {"ocr_provider": args.ocr_provider} if args.ocr_provider else {}
        job = await service.submit(args.source, template=args.template, options=options)
        if args.run:
            job = await service.run(job)
        print(json.dumps(job.model_dump(mode="json"), ensure_ascii=False, indent=2))
    elif args.command == "status":
        job = store.get_ingestion_job(args.job_id)
        if not job:
            raise SystemExit(f"unknown job: {args.job_id}")
        print(json.dumps(job.model_dump(mode="json"), ensure_ascii=False, indent=2))
    elif args.command == "questions":
        print(json.dumps([item.model_dump(mode="json") for item in store.get_questions(args.job_id)], ensure_ascii=False, indent=2))
    elif args.command == "answer":
        questions = store.get_questions(args.job_id)
        question = next((item for item in questions if item.id == args.question_id), None)
        if not question:
            raise SystemExit(f"unknown question: {args.question_id}")
        question.answer = args.value
        question.status = "answered"
        store.save_question(question)
        print(json.dumps(question.model_dump(mode="json"), ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Evidence-preserving unified ingestion")
    parser.add_argument("--db", default="historybot.db")
    sub = parser.add_subparsers(dest="command", required=True)
    submit = sub.add_parser("submit")
    submit.add_argument("source")
    submit.add_argument("--template", default="historical")
    submit.add_argument("--ocr-provider", choices=["rapidocr", "trocr-printed", "trocr-handwritten", "none"])
    submit.add_argument("--run", action="store_true")
    status = sub.add_parser("status")
    status.add_argument("job_id")
    questions = sub.add_parser("questions")
    questions.add_argument("job_id")
    answer = sub.add_parser("answer")
    answer.add_argument("job_id")
    answer.add_argument("question_id")
    answer.add_argument("--value", required=True)
    asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    main()
