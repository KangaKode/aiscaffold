# Tutorial: Generate and Run Your First Roundtable Project

This walkthrough starts from the public scaffold repository and ends with a generated project that can run tests, a local demo, and the FastAPI gateway.

> Time: 20-30 minutes
> Prerequisites: Python 3.12+, Git, and `pip`. API keys are only required for live LLM calls; the demo path uses mock agents.

## 1. Install Copier

`roundtable` is a Copier template. Install Copier in the environment where you want to create the generated project:

```bash
python3 -m pip install copier
```

## 2. Generate a Project

Run Copier from the directory where you keep projects:

```bash
copier copy gh:KangaKode/roundtable roundtable-demo --trust
```

Copier asks for project metadata such as name, slug, project type, LLM provider, and persistence. The scaffold intentionally creates a generated application directory inside the destination, using the `project_slug` value.

For example, if you choose `Roundtable Demo` as the project name, the generated app is usually here:

```bash
cd roundtable-demo/roundtable_demo
```

If you accept the default slug, use the `cd <project_slug>` line printed by Copier at the end of generation.

## 3. Create an Environment

```bash
python3 -m venv venv
source venv/bin/activate
python -m pip install -r requirements.txt
```

Optional but recommended:

```bash
pre-commit install
```

## 4. Run the Local Demo

The demo does not require API keys or network calls:

```bash
make demo
```

You should see mock agents deliberate through the round table protocol: strategy, independent analysis, challenge, synthesis, and voting.

## 5. Run the Checks

```bash
make test
make doctor
```

`make test` runs the generated project's pytest suite. `make doctor` checks architecture rules, red-team checks, linters, and documentation freshness where available.

## 6. Configure Live LLM Calls

Copy the environment example and add the provider key you selected during generation:

```bash
cp .env.example .env
```

Examples:

```bash
ANTHROPIC_API_KEY=your-key-here
OPENAI_API_KEY=your-key-here
GOOGLE_API_KEY=your-key-here
```

The generated LLM client is provider-aware and keeps the stable prompt prefix separate from dynamic user content so providers with prompt caching can reduce repeated input-token cost.

## 7. Create a Custom Agent

Use the generated helper:

```bash
make new-agent NAME=my_analyst DOMAIN="code review"
```

Then edit the generated agent in `src/<project_slug>/agents/my_analyst.py`. Agents implement the same three core capabilities whether they are local Python classes or remote HTTP services:

- `analyze`: independent evidence-backed analysis
- `challenge`: review another agent's findings with counter-evidence
- `vote`: approve, approve with conditions, or dissent from the synthesis

## 8. Start the API Gateway

If you included the API gateway option:

```bash
make serve
```

Open `http://localhost:8000/docs` for the generated OpenAPI documentation.

## Troubleshooting

| Symptom | Likely Cause | Fix |
|---------|--------------|-----|
| `copier: command not found` | Copier is not installed in the active environment | `python3 -m pip install copier` |
| `cd my-project` does not contain source files | The app is inside the generated `project_slug` directory | Use the `cd <project_slug>` line printed after generation |
| `make test` cannot find dependencies | Requirements were not installed in the active virtual environment | Activate `venv`, then run `python -m pip install -r requirements.txt` |
| API calls fail because no key is configured | `.env` is missing provider credentials | Copy `.env.example` to `.env` and add the selected provider key |
| Architecture tests fail after edits | A lower layer imports from a higher layer | See [ARCHITECTURE.md](ARCHITECTURE.md) for dependency direction rules |
