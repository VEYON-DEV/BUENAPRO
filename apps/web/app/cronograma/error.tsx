"use client";
import { TimelineFeedback } from "@/features/timeline";
export default function Error({ reset }: { reset: () => void }) { return <TimelineFeedback retry={reset} />; }
