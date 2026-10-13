"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";

/** Re-render the server component every `ms` while something is still moving. */
export function AutoRefresh({ active, ms = 1000 }: { active: boolean; ms?: number }) {
  const router = useRouter();
  useEffect(() => {
    if (!active) return;
    const timer = setInterval(() => router.refresh(), ms);
    return () => clearInterval(timer);
  }, [active, ms, router]);
  return null;
}
