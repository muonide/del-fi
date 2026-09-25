# DAWN-CHORUS — The Listening Station

Salmonberry Creek Preserve has an automatic listening station that identifies birds by their songs and calls around the clock, and DAWN-CHORUS reports its detections over the mesh. This page explains how it works, what it can and cannot hear, and how far to trust it.

---

## How It Works

### The Listening Station

A weatherproof microphone hangs under the eaves of the Beaver Pond Blind, wired to a Raspberry Pi running BirdNET-Pi, free open-source software. It listens continuously and analyses the sound in 3-second slices. Every slice that sounds like a bird is identified to species with a confidence score, and detections scoring at least 0.7 are logged. Once a minute, the latest detections are summarised for DAWN-CHORUS.

### BirdNET

BirdNET is a machine-learning model developed by the Cornell Lab of Ornithology and Chemnitz University of Technology that recognises the sounds of thousands of bird species worldwide. It is the same technology behind the BirdNET app. It works from sound alone, so it identifies birds you would never see, including owls at night and rails deep in the cattails.

### What It Can Hear

The microphone hears the Beaver Pond, the cattail marsh, the far end of the Marsh Boardwalk and the edge of the Cedar Forest, up to roughly 100 yards for loud singers and much less for quiet ones. It cannot hear the Creek Loop at Alder Bend, the Sedge Meadow or the Snag Overlook. A bird the station has not detected may still be on the preserve.

---

## Live Detections

### What the Node Reports

DAWN-CHORUS keeps five live facts from the station, shown by !data or by asking in plain words. Singing now: species detected in the last 15 minutes. Birds detected today: how many species since midnight and the most frequent ones. Owls tonight: owls detected since 6 pm (or last night, if asked in the morning). Dawn chorus: songbirds heard between 4 and 9 am. New arrivals: species heard in the last 14 days that had not been heard for at least four months, such as returning migrants. Each fact shows its age; if the station stops reporting, facts are marked STALE rather than shown as current.

### How Accurate Is It? Confidence Scores

How accurate the station is depends on the detection. Every detection has a confidence score from 0 to 1. The station ignores anything below 0.7. A species detected many times with scores above 0.9 is almost certainly right. A single detection of a rare or out-of-season species, even with a high score, needs confirmation by a person who saw or recorded it.

---

## When the Station Gets It Wrong

### Common False Detections

Steller's Jays imitate Red-tailed Hawks so well that the station sometimes logs a hawk that was a jay. Wind, heavy rain, dripping eaves and passing aircraft can produce nonsense detections. Two birds singing at once can be merged or missed. People whistling or playing recordings are detected as the bird they imitate, which is one more reason not to use playback at the preserve.

### Privacy

The station is set to discard any recording in which it detects human speech, and only short clips of bird sounds are kept, for checking identifications. It does not record conversations. The microphone's location is marked on the trailhead map.
