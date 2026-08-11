import { z } from "zod";

export const loginSchema = z.object({
  username: z.string().min(1).max(64),
  password: z.string().min(1).max(128),
});

export const setupPasswordSchema = z
  .object({
    password: z.string().min(14).max(128),
    confirmation: z.string().min(14).max(128),
  })
  .refine((value) => value.password === value.confirmation, {
    message: "两次输入的密码不一致",
    path: ["confirmation"],
  });

export const passwordActionExchangeSchema = z.object({
  token: z.string().min(32).max(256),
});

export type LoginInput = z.infer<typeof loginSchema>;
export type SetupPasswordInput = z.infer<typeof setupPasswordSchema>;
export type PasswordActionExchangeInput = z.infer<
  typeof passwordActionExchangeSchema
>;
