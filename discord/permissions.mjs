// Guild-scoped commands are available to every member of this server.
export function canControl(guildId, configuredGuildId) {
  return Boolean(guildId && guildId === configuredGuildId);
}
