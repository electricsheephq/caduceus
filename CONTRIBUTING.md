# Contributing to Caduceus

Thanks for helping. A few rules keep the plugin safe and simple.

- **No real personal data, ever.** Use the synthetic demo persona (`caduceus/demo/`) for tests, examples and bug reports.
  Never commit a real DNA file, a real Apple Health export, a real report, or text copied from SNPedia.
- **Standard library only:** no new dependencies and no build step.
- **Tests:** run `python -m pip install pytest`, then `python -m pytest`. The tests never use the network.
- **Before changing a module,** read its contract in [docs/contracts.md](docs/contracts.md). [AGENTS.md](AGENTS.md) has
  the full rules, for people and coding agents alike.
- **Health statements are information with limits.** Never word a result as a recommendation, and don't overstate small
  genetic effects.
- **Bugs and ideas:** open an issue, using the [bug form](https://github.com/electricsheephq/caduceus/issues/new?template=bug_report.yml)
  for bugs. Report security problems through the [private advisory form](https://github.com/electricsheephq/caduceus/security/advisories/new).

Contributions are licensed under Apache-2.0, like the rest of the project.
