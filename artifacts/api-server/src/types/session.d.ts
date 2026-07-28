import "express-session";

export interface DiscordGuild {
  id: string;
  name: string;
  icon: string | null;
  owner: boolean;
  permissions: string;
  features: string[];
}

declare module "express-session" {
  interface SessionData {
    userId: string;
    username: string;
    globalName: string | null;
    avatar: string | null;
    accessToken: string;
    guilds: DiscordGuild[];
  }
}
