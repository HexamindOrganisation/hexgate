import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export function initials(name: string): string {
  return name
    .split(/\s+/)
    .map((w) => w[0])
    .join("")
    .slice(0, 2)
    .toUpperCase();
}

const EUR = new Intl.NumberFormat("en-IE", { style: "currency", currency: "EUR" });

/** 12400 as "€12,400.00". */
export function eur(amount: number): string {
  return EUR.format(amount);
}

/** "FR76 4242 4000 ..." and "fr7642424000..." compare equal, as the bank compares them. */
export function normalizeIban(iban: string): string {
  return iban.replace(/\s+/g, "").toUpperCase();
}
