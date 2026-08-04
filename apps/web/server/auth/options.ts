import type { NextAuthOptions } from "next-auth";
import CredentialsProvider from "next-auth/providers/credentials";
import { query } from "@/server/db/client";
import { ensureServerEnv } from "@/server/env";

ensureServerEnv();

export const authOptions: NextAuthOptions = {
  session: { strategy: "jwt" },
  secret: process.env.NEXTAUTH_SECRET ?? process.env.AUTH_SECRET,
  callbacks: {
    async jwt({ token, user }) {
      if (user) {
        token.userId = user.id;
        token.tenantId = (user as { tenantId?: string }).tenantId;
        token.role = (user as { role?: string }).role;
      }
      return token;
    },
    async session({ session, token }) {
      if (session.user) {
        session.user.id = String(token.userId);
        session.user.tenantId = String(token.tenantId);
        session.user.role = String(token.role ?? "member");
      }
      return session;
    },
  },
  providers: [
    CredentialsProvider({
      name: "Usuario",
      credentials: {
        email: { label: "Usuario", type: "text" },
        password: { label: "Contrasena", type: "password" },
        name: { label: "Nombre", type: "text" },
        tenant_name: { label: "Empresa", type: "text" },
      },
      async authorize(credentials) {
        const email = credentials?.email?.trim().toLowerCase();
        if (!email) return null;
        const password = credentials?.password?.trim();
        if (!password) return null;
        const name = credentials?.name?.trim() || email;
        const tenantName = credentials?.tenant_name?.trim();
        const user = await upsertUserWithTenant(email, password, name, tenantName);
        if (!user) return null;
        return {
          id: user.id,
          email: user.email,
          name: user.name,
          tenantId: user.tenant_id,
          role: user.role,
        };
      },
    }),
  ],
};

async function upsertUserWithTenant(email: string, password: string, name: string, tenantName?: string) {
        const existingUser = await query<{
    id: string;
    email: string;
    name: string;
    password_ok: boolean;
  }>(
    `
    SELECT id, email, name, password_hash = crypt($2, password_hash) AS password_ok
    FROM users
    WHERE email = $1 AND password_hash IS NOT NULL
    LIMIT 1
    `,
    [email, password],
  );
  if (existingUser.rows[0] && !existingUser.rows[0].password_ok) return null;

  const userResult = await query<{
    id: string;
    email: string;
    name: string;
  }>(
    `
    INSERT INTO users (email, name, password_hash)
    VALUES ($1, $2, crypt($3, gen_salt('bf')))
    ON CONFLICT (email)
    DO UPDATE SET
      name = COALESCE(EXCLUDED.name, users.name),
      password_hash = COALESCE(users.password_hash, EXCLUDED.password_hash),
      updated_at = now()
    RETURNING id, email, name
    `,
    [email, name, password],
  );
  const user = userResult.rows[0];

  const existingMembership = await query<{
    tenant_id: string;
    role: string;
  }>(
    `
    SELECT tenant_id, role
    FROM tenant_members
    WHERE user_id = $1
    ORDER BY created_at
    LIMIT 1
    `,
    [user.id],
  );

  if (existingMembership.rows[0]) {
    return { ...user, ...existingMembership.rows[0] };
  }

  const tenantResult = await query<{ id: string }>(
    `
    INSERT INTO tenants (name)
    VALUES ($1)
    RETURNING id
    `,
    [tenantName || `${name} workspace`],
  );
  const tenantId = tenantResult.rows[0].id;
  await query(
    `
    INSERT INTO tenant_members (tenant_id, user_id, role)
    VALUES ($1, $2, 'owner')
    ON CONFLICT (tenant_id, user_id) DO NOTHING
    `,
    [tenantId, user.id],
  );
  return { ...user, tenant_id: tenantId, role: "owner" };
}
