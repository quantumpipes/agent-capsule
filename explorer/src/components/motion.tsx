// Copyright 2026 Quantum Pipes Technologies, LLC
// SPDX-License-Identifier: Apache-2.0

/**
 * Motion primitives for the marketing site.
 * Wraps framer-motion with presets tuned for the QP design system.
 */

import {
  motion,
  AnimatePresence,
  useInView,
  type Variants,
} from "motion/react";
import { useRef, type ReactNode } from "react";

// ============================================================================
// Re-exports
// ============================================================================

export { motion, AnimatePresence };

// ============================================================================
// Spring presets
// ============================================================================

export const springs = {
  gentle: { type: "spring" as const, stiffness: 120, damping: 20 },
  snappy: { type: "spring" as const, stiffness: 300, damping: 30 },
  bouncy: { type: "spring" as const, stiffness: 400, damping: 25 },
  smooth: { type: "spring" as const, stiffness: 200, damping: 30 },
};

// ============================================================================
// Variant presets
// ============================================================================

export const fadeUp: Variants = {
  hidden: { opacity: 0, y: 24 },
  visible: { opacity: 1, y: 0 },
};

export const fadeIn: Variants = {
  hidden: { opacity: 0 },
  visible: { opacity: 1 },
};

export const scaleIn: Variants = {
  hidden: { opacity: 0, scale: 0.95 },
  visible: { opacity: 1, scale: 1 },
};

export const slideInLeft: Variants = {
  hidden: { opacity: 0, x: -30 },
  visible: { opacity: 1, x: 0 },
};

export const slideInRight: Variants = {
  hidden: { opacity: 0, x: 30 },
  visible: { opacity: 1, x: 0 },
};

export const staggerContainer: Variants = {
  hidden: {},
  visible: {
    transition: {
      staggerChildren: 0.1,
    },
  },
};

export const staggerContainerSlow: Variants = {
  hidden: {},
  visible: {
    transition: {
      staggerChildren: 0.15,
    },
  },
};

// ============================================================================
// useReveal hook - triggers animation when element scrolls into view
// ============================================================================

export function useReveal(options?: { once?: boolean; margin?: string; amount?: number }) {
  const ref = useRef<HTMLDivElement>(null);
  const isInView = useInView(ref, {
    once: options?.once ?? true,
    margin: (options?.margin ?? "-80px") as `${number}px`,
    amount: options?.amount ?? 0.2,
  });
  return { ref, isInView };
}

// ============================================================================
// Reveal component - declarative scroll reveal wrapper
// ============================================================================

interface RevealProps {
  children: ReactNode;
  className?: string;
  delay?: number;
  direction?: "up" | "down" | "left" | "right" | "none";
  duration?: number;
  once?: boolean;
}

export function Reveal({
  children,
  className,
  delay = 0,
  direction = "up",
  duration = 0.6,
  once = true,
}: RevealProps) {
  const ref = useRef<HTMLDivElement>(null);
  const isInView = useInView(ref, { once, margin: "-60px", amount: 0.2 });

  const directionMap = {
    up: { y: 30 },
    down: { y: -30 },
    left: { x: 30 },
    right: { x: -30 },
    none: {},
  };

  return (
    <motion.div
      ref={ref}
      className={className}
      initial={{ opacity: 0, ...directionMap[direction] }}
      animate={isInView ? { opacity: 1, x: 0, y: 0 } : { opacity: 0, ...directionMap[direction] }}
      transition={{ duration, delay, ease: [0.16, 1, 0.3, 1] }}
    >
      {children}
    </motion.div>
  );
}

// ============================================================================
// StaggerReveal - container that staggers children on scroll
// ============================================================================

interface StaggerRevealProps {
  children: ReactNode;
  className?: string;
  staggerDelay?: number;
  once?: boolean;
}

export function StaggerReveal({
  children,
  className,
  staggerDelay = 0.1,
  once = true,
}: StaggerRevealProps) {
  const ref = useRef<HTMLDivElement>(null);
  const isInView = useInView(ref, { once, margin: "-60px", amount: 0.15 });

  return (
    <motion.div
      ref={ref}
      className={className}
      initial="hidden"
      animate={isInView ? "visible" : "hidden"}
      variants={{
        hidden: {},
        visible: { transition: { staggerChildren: staggerDelay } },
      }}
    >
      {children}
    </motion.div>
  );
}

// ============================================================================
// cardReveal — per-card viewport trigger
//
// `StaggerReveal` fires every child as soon as the container enters view.
// On a long grid, that means cards still 800px below the fold animate
// before the user has seen them — so when they DO scroll, the motion is
// over and the cards just sit there. The page feels static below the
// first viewport.
//
// `cardReveal(index)` gives every card its own viewport trigger via
// `whileInView`, so each card animates the moment IT crosses the fold.
// Cards in the same row still feel staggered: the small `index * 0.06s`
// delay creates a left-to-right cascade for cards that enter together,
// while cards far apart vertically each get their own clean entrance.
//
// The delay caps at index 3 so a 12-card grid doesn't have card 12
// waiting 0.72s after entering view.
//
// Usage:
//   <div className="grid md:grid-cols-3 gap-8">
//     {items.map((item, i) => (
//       <motion.div key={item.id} {...cardReveal(i)}>
//         ...card...
//       </motion.div>
//     ))}
//   </div>
// ============================================================================

export function cardReveal(index: number = 0, options?: { delay?: number; y?: number }) {
  return {
    initial: { opacity: 0, y: options?.y ?? 16 },
    whileInView: { opacity: 1, y: 0 },
    // Trigger when 15% of the element has crossed the viewport bottom.
    // No top/bottom margin gymnastics — that adds layout work without
    // improving perceived motion. `once: true` releases the observer
    // after firing, which matters on long pages with many cards.
    viewport: { once: true, amount: 0.15 } as const,
    transition: {
      duration: 0.45,
      delay: (options?.delay ?? 0) + Math.min(index, 3) * 0.05,
      ease: [0.16, 1, 0.3, 1] as [number, number, number, number],
    },
  };
}

// ============================================================================
// DrawPath - animates an SVG path drawing
// ============================================================================

interface DrawPathProps {
  d: string;
  className?: string;
  duration?: number;
  delay?: number;
  strokeWidth?: number;
}

export function DrawPath({
  d,
  className,
  duration = 0.8,
  delay = 0.5,
  strokeWidth = 3,
}: DrawPathProps) {
  return (
    <motion.path
      d={d}
      stroke="currentColor"
      strokeWidth={strokeWidth}
      fill="none"
      strokeLinecap="round"
      className={className}
      initial={{ pathLength: 0, opacity: 0 }}
      animate={{ pathLength: 1, opacity: 1 }}
      transition={{ duration, delay, ease: [0.16, 1, 0.3, 1] }}
    />
  );
}
