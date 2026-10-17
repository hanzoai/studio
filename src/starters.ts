/**
 * The starters: what the home offers under the composer and Explore lists in
 * full. A starter is a kind and a first message; a press starts a conversation
 * with it. The home shows only starters whose kind can run now; Explore shows
 * every one, and says what each needs.
 *
 * `thumb` is the card's picture, made once by our own models and kept in the
 * public `hanzo-studio` bucket at catalog/<id>/<sha8>.webp (scripts/thumbs.ts);
 * a starter without one is drawn as type.
 */
import type { KindId } from './made.ts'

export type Category = 'image' | 'edit' | 'poster' | 'document' | 'slides' | 'type' | 'mesh' | 'video' | 'ad' | 'audio' | 'fashion'

export const CATEGORIES: readonly { id: Category | 'all'; label: string }[] = [
  { id: 'all', label: 'All' },
  { id: 'image', label: 'Image' },
  { id: 'edit', label: 'Edit' },
  { id: 'poster', label: 'Posters' },
  { id: 'document', label: 'Documents' },
  { id: 'slides', label: 'Slides' },
  { id: 'type', label: 'Type' },
  { id: 'mesh', label: '3D' },
  { id: 'video', label: 'Video' },
  { id: 'ad', label: 'Ads' },
  { id: 'audio', label: 'Audio' },
  { id: 'fashion', label: 'Fashion' },
]

export interface Starter {
  id: string
  kind: KindId
  category: Category
  title: string
  subtitle: string
  /** The first message. */
  prompt: string
  /** Whether it starts from a picture the person attaches. */
  picture?: boolean
  thumb?: string
}

/** Roughly how long one takes, for the card's corner. */
export const RUNTIME: Record<KindId, string> = {
  image: '~20s',
  poster: '~30s',
  document: '~40s',
  slides: '~60s',
  type: '~15s',
  mesh: '~2m',
  video: '~3m',
  ad: '~45s',
  voice: '~5s',
  music: '~1m',
}

export const STARTERS: readonly Starter[] = [
  { id: 'poster-jazz', kind: 'poster', category: 'poster', title: 'Jazz night poster', subtitle: 'A3, full bleed', prompt: 'A poster for a jazz night this Friday at 9pm at The Blue Room, 14 Canal Street. Smoky, warm, late-night. Free entry before 10.' },
  { id: 'poster-launch', kind: 'poster', category: 'poster', title: 'Product launch poster', subtitle: '18 × 24, framed', prompt: 'An 18x24 launch poster for Aurora, a minimalist desk lamp with a warm dimmable glow. Launching 12 November. Pre-order at aurora.lighting.' },
  { id: 'poster-talk', kind: 'poster', category: 'poster', title: 'Conference talk', subtitle: 'Type only, A2', prompt: 'A type-only A2 poster for a talk called "Small models, big systems" by Dr. Lena Ortiz, Hall B, 3 December, 14:00.' },
  { id: 'poster-story', kind: 'poster', category: 'poster', title: 'Gallery opening story', subtitle: 'Instagram story', prompt: 'An Instagram story poster for the opening of "Salt & Light", a photography show at Gallery Nine, Saturday 6–9pm.' },
  { id: 'image-product', kind: 'image', category: 'image', title: 'Product on marble', subtitle: 'Studio product shot', prompt: 'A studio product photograph of a matte black ceramic coffee cup on white marble, soft window light from the left, shallow depth of field, 4:5.' },
  { id: 'image-portrait', kind: 'image', category: 'image', title: 'Cinematic portrait', subtitle: 'Golden hour, 85mm', prompt: 'A cinematic portrait of an elderly fisherman mending a net at golden hour, 85mm lens, warm rim light, film grain.' },
  { id: 'image-iso', kind: 'image', category: 'image', title: 'Isometric city block', subtitle: 'Illustration', prompt: 'An isometric illustration of a small city block at dusk: a bakery, a bike shop, a rooftop garden, soft pastel palette, clean lines, 1:1.' },
  { id: 'image-food', kind: 'image', category: 'image', title: 'Breakfast flat lay', subtitle: 'Overhead food shot', prompt: 'An overhead flat lay of a breakfast table: sourdough toast, berries, coffee, linen napkin, natural light, 1:1.' },
  { id: 'edit-background', kind: 'image', category: 'edit', title: 'Clean background', subtitle: 'From your photo', prompt: 'Put this product on a clean, seamless light grey studio background with a soft shadow.', picture: true },
  { id: 'edit-season', kind: 'image', category: 'edit', title: 'Change the season', subtitle: 'From your photo', prompt: 'Make this scene winter: fresh snow, cold blue light, breath in the air. Keep everything else the same.', picture: true },
  { id: 'edit-wide', kind: 'image', category: 'edit', title: 'Widen to 16:9', subtitle: 'Expand the frame', prompt: 'Expand this picture to 16:9, continuing the scene naturally on both sides.', picture: true },
  { id: 'doc-proposal', kind: 'document', category: 'document', title: 'Project proposal', subtitle: 'PDF and Word', prompt: 'A two-page proposal from Northwind Studio to redesign the website of Harbor Dental: scope, timeline over six weeks, team, and a fixed price of $18,000.' },
  { id: 'doc-onepager', kind: 'document', category: 'document', title: 'Product one-pager', subtitle: 'With a cover picture', prompt: 'A one-pager for Aurora, a dimmable desk lamp: the problem, three features, specs table, and price. Include a product picture.' },
  { id: 'doc-invoice', kind: 'document', category: 'document', title: 'Invoice', subtitle: 'Line items, totals', prompt: 'An invoice from Northwind Studio to Harbor Dental for website design: discovery $2,000, design $9,000, build $7,000, due in 30 days.' },
  { id: 'doc-press', kind: 'document', category: 'document', title: 'Press release', subtitle: 'Announcement', prompt: 'A press release announcing that Greenline Bikes opens its third store, in Portland, on 1 December, with a quote from the founder.' },
  { id: 'deck-pitch', kind: 'slides', category: 'slides', title: 'Seed pitch deck', subtitle: 'Ten slides, PowerPoint', prompt: 'A ten-slide seed pitch deck for Ledgerly, bookkeeping on autopilot for freelancers: problem, solution, market, traction chart, business model, team, the ask of $2.5M.' },
  { id: 'deck-qbr', kind: 'slides', category: 'slides', title: 'Quarterly review', subtitle: 'With a chart', prompt: 'A quarterly business review deck for Q3: revenue by month July 120k, August 138k, September 151k; highlights, risks, and next-quarter goals.' },
  { id: 'deck-lesson', kind: 'slides', category: 'slides', title: 'Workshop lesson', subtitle: 'Pictures and steps', prompt: 'A short workshop deck teaching beginners how to make cold brew coffee at home, step by step, with pictures.' },
  { id: 'type-roaster', kind: 'type', category: 'type', title: 'Coffee roaster brand', subtitle: 'Warm and crafted', prompt: 'A type system for a small-batch coffee roaster: warm, crafted, a little playful, readable on bags and menus.' },
  { id: 'type-editorial', kind: 'type', category: 'type', title: 'Editorial magazine', subtitle: 'Serif-led', prompt: 'A type system for an independent long-form magazine: serif-led, calm, generous reading text, a confident display face.' },
  { id: 'type-dev', kind: 'type', category: 'type', title: 'Developer tool', subtitle: 'Precise, dark', prompt: 'A type system for a developer tool: precise, dark by default, a grotesk with a mono for code, one electric accent.' },
  { id: 'mesh-sneaker', kind: 'mesh', category: 'mesh', title: 'Sneaker model', subtitle: 'glTF, from words', prompt: 'A 3D model of a white leather low-top sneaker with a gum sole.' },
  { id: 'mesh-vase', kind: 'mesh', category: 'mesh', title: 'Ceramic vase', subtitle: 'glTF, from a picture', prompt: 'Make a 3D model of the object in this picture.', picture: true },
  { id: 'video-turntable', kind: 'video', category: 'video', title: 'Product turntable', subtitle: '5s, 1:1', prompt: 'A 5 second clip: a matte black coffee cup slowly rotating on a turntable, studio light, 1:1.' },
  { id: 'video-ocean', kind: 'video', category: 'video', title: 'Ocean social clip', subtitle: '5s, 9:16', prompt: 'A 5 second vertical clip of waves rolling onto a black sand beach at dawn, slow push in.' },
  { id: 'ad-sale', kind: 'ad', category: 'ad', title: 'Summer sale ad set', subtitle: 'Social + banners', prompt: 'An ad set for a summer sale at Greenline Bikes: 30% off city bikes this weekend only. Shop now.' },
  { id: 'ad-app', kind: 'ad', category: 'ad', title: 'App install ads', subtitle: 'Every size', prompt: 'An ad set for Ledgerly, an app that does bookkeeping for freelancers: "Your books, done while you work". Get the app.' },
  { id: 'voice-intro', kind: 'voice', category: 'audio', title: 'Podcast intro', subtitle: 'Voiceover', prompt: 'A warm 15 second podcast intro voiceover for "Small Hours", a show about people who work nights.' },
  { id: 'voice-ad', kind: 'voice', category: 'audio', title: 'Radio spot', subtitle: '30 second read', prompt: 'A 30 second radio ad read for Greenline Bikes summer sale, upbeat and friendly.' },
  { id: 'music-lofi', kind: 'music', category: 'audio', title: 'Lo-fi bed', subtitle: 'Instrumental', prompt: 'A 60 second lo-fi hip hop instrumental bed, mellow keys, vinyl crackle, 80 bpm.' },
  { id: 'fashion-ghost', kind: 'image', category: 'fashion', title: 'CAD to ghost mannequin', subtitle: 'From a flat drawing', prompt: 'Turn this garment drawing into a photorealistic ghost-mannequin product photo on white, true to its colours, print and proportions.', picture: true },
  { id: 'fashion-product', kind: 'image', category: 'fashion', title: 'Product photo', subtitle: 'From a flat lay', prompt: 'Turn this flat lay into a clean e-commerce product photo on a light grey background with a soft shadow.', picture: true },
  { id: 'fashion-swap', kind: 'image', category: 'fashion', title: 'Outfit swap', subtitle: 'Keep the model', prompt: 'Dress the person in this photo in a camel wool overcoat and a cream turtleneck. Keep their face, pose, light and background exactly.', picture: true },
]
