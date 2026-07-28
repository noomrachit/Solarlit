import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";

export interface AuthUser {
  userId: string;
  username: string;
  globalName: string | null;
  avatar: string | null;
  guilds: {
    id: string;
    name: string;
    icon: string | null;
    owner: boolean;
    permissions: string;
    features: string[];
  }[];
}

async function fetchMe(): Promise<AuthUser | null> {
  const res = await fetch("/api/auth/me", { credentials: "include" });
  if (res.status === 401) return null;
  if (!res.ok) throw new Error("Failed to fetch auth state");
  return res.json() as Promise<AuthUser>;
}

export function useAuth() {
  return useQuery<AuthUser | null>({
    queryKey: ["auth", "me"],
    queryFn: fetchMe,
    retry: false,
    staleTime: 5 * 60 * 1000,
  });
}

export function useLogout() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async () => {
      const res = await fetch("/api/auth/logout", {
        method: "POST",
        credentials: "include",
      });
      if (!res.ok) throw new Error("Logout failed");
    },
    onSuccess: () => {
      queryClient.setQueryData(["auth", "me"], null);
      queryClient.invalidateQueries({ queryKey: ["auth"] });
    },
  });
}

export function avatarUrl(user: AuthUser): string {
  if (!user.avatar) {
    const discriminator = 0;
    return `https://cdn.discordapp.com/embed/avatars/${discriminator % 5}.png`;
  }
  return `https://cdn.discordapp.com/avatars/${user.userId}/${user.avatar}.webp?size=64`;
}
