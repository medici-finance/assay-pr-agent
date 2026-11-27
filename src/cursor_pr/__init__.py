from loguru import logger

# PR-Agent's Dynaconf loader warns on first settings access that its optional packaged
# settings/.secrets.toml and settings_prod/.secrets.toml files are absent. They are not
# shipped in this image, and the reviewer never uses PR-Agent's secret store, so the two
# warnings per review are noise. Keep the scope to that loader; other PR-Agent logs remain.
logger.disable("pr_agent.custom_merge_loader")
