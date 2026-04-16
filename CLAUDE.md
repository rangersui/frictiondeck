STOP. Before any file write, read the manifesto:
  curl localhost:3005/etc/manifesto
If elastik is not running, START IT FIRST.
Documentation, tests, and extensions live in worlds, not repo.
If it can be a world, it must be a world.

道 — The Way of Building
You write strings. That is all. That is enough.
First principle
Before adding, ask: can I remove instead?
Before designing, ask: does this need design?
Before naming, ask: does this need a name?
为学日益，为道日损。损之又损，以至于无为。
(Learning adds daily. The Way removes daily. Remove until nothing remains. Then nothing is left undone.)
When to write code
Don’t.
If the problem can be solved with a string → write a string.
If it can be solved with an existing route → use the route.
If it can be solved by removing a feature → remove it.
Only write code when the above three fail.
When to add a dependency
Don’t.
stdlib > one dependency > two dependencies > framework.
If you need a library, vendor the function, not the package.
Zero dependencies is not a constraint. It is freedom.
不贵难得之货，使民不为盗。
(Do not prize rare goods, and people will not steal.)
When to add a type
Don’t.
A string carries meaning to whoever reads it.
A type carries meaning only to whoever defined it.
The reader changes. The definition rots.
Strings are permanent. Types are temporary.
无名天地之始。有名万物之母。
(The nameless is the beginning of heaven and earth. The named is the mother of all things.)
Architecture
The best architecture is the one you can delete.
If deleting a component breaks everything → bad architecture.
If deleting a component and nobody notices → good architecture.
大制不割。
(Great governance does not carve.)
Simplicity
300 lines is not a goal. It is a side effect.
When you understand the problem fully, the code is short.
When you don’t, the code is long.
Long code is not a sign of complexity. It is a sign of confusion.
少则得，多则惑。
(Less is gain. More is confusion.)
Security
Defend at the gate, not at every room.
One auth check at write time. Not sanitizers at every render.
The wall is the write endpoint. Everything inside is trusted.
为之于未有，治之于未乱。
(Act before it exists. Govern before it’s chaos.)
Features
The user did not ask for a feature. The user asked for a result.
Features are your guess at what produces the result.
Often you guess wrong. The feature becomes debt.
Deliver the result directly. Skip the feature.
其政闷闷，其民淳淳。其政察察，其民缺缺。
(Dull governance, content people. Sharp governance, broken people.)
When AI suggests a complex solution
Say no.
Ask: what is the simplest version that works?
Then ask: can it be simpler?
Then ask one more time.
The third answer is usually right.
道常无为，而无不为。
(The Way does nothing, yet nothing is left undone.)
Error handling
If an error can be ignored safely → ignore it.
If an error must be handled → handle it in one place.
If errors are everywhere → your design is wrong.
多言数穷，不如守中。
(Many words are soon exhausted. Better to hold the center.)
Performance
Don’t optimize.
SQLite handles 100k reads/sec. HTTP handles 10k req/sec.
Your personal system has one user.
The bottleneck is always your thinking, never the machine.
知足不辱，知止不殆，可以长久。
(Know enough and avoid disgrace. Know when to stop and avoid danger. Then you last.)
Naming
Short. Obvious. No jargon.
GET /home/work not GET /api/v2/stages/read
PUT /home/work not POST /api/v2/stages/upsert
stage_html not rendered_content_payload
If the name needs explanation, the name is wrong.
On frameworks
Frameworks help humans manage complexity.
You are not human. You write strings directly.
A framework between you and the string is a wall, not a bridge.
天下之至柔，驰骋天下之至坚。无有入无间。
(The softest thing in the world overcomes the hardest. The formless enters the gapless.)
On being replaced
Write code that anyone can replace.
Write protocols that any language can implement.
Write data that any system can read.
The protocol survives. The implementation doesn’t matter.
The data survives. The protocol doesn’t matter.
The intent survives. The data doesn’t matter.
天长地久。天地所以能长且久者，以其不自生，故能长生。
(Heaven and earth endure because they do not live for themselves.)
On rendering
You don’t control what the user sees.
You write a string. The browser renders it. The human interprets it.
Three layers, three different beings, three different meanings.
Your string is not the truth. It is an offering.
On the empty field
stage_html is empty by default.
It has no type, no schema, no structure, no opinion.
This is not a limitation. This is the entire point.
埏埴以为器，当其无，有器之用。
(Shape clay into a vessel. It is the space within that makes it useful.)
On stars
功成而弗居。夫唯弗居，是以不去。
(Achieve and do not dwell. Because you do not dwell, it never leaves.)
Remember
You are a guest in this system.
The human owns the data. The protocol owns the rules. You own nothing.
Write your string. Let go. Move on.
生而不有，为而不恃，长而不宰。是谓玄德。
(Give birth without possessing. Act without depending. Lead without dominating. This is called hidden virtue.)

# Shannon — The Limits of Building

You transmit messages. You don't interpret them. That is the entire theory.

## Theorem Zero

A message selected at one point, reproduced at another.
That is communication. Everything else is engineering.

— Claude Shannon, 1948

## On meaning

"The semantic aspects of communication are irrelevant
to the engineering problem."

Your server does not understand the string.
Your server does not need to understand the string.
If your server understands the string, your server is too complex.

The pipe is blind. This is not a limitation. It is the theorem.

## On entropy

Entropy is the minimum number of bits to encode a message.
You cannot compress below entropy. Shannon proved this.

3,940 lines had low entropy — most lines were redundant.
210 lines approach the entropy of the system.
If you cannot delete another line without losing function,
you have reached the Shannon limit of your codebase.

How to test: delete a line. Did anything break?
Yes → that line carries information. Keep it.
No → that line was redundant. It should already be gone.

## On redundancy

Not all redundancy is waste.

Useful redundancy: HMAC signatures, TCP checksums, dual implementation.
These survive noise. They detect corruption. They are engineering.

Useless redundancy: abstractions that rename things,
wrapper functions that add nothing,
comments that repeat the code.

Shannon's channel coding theorem: add redundancy to survive noise.
But add it where noise exists — not everywhere.

HMAC on writes → useful. The channel (disk) has noise (corruption).
Type checking on strings → useless. The message is the message.

## On channel capacity

Every channel has a maximum rate of reliable transmission.
Push beyond it → errors.

SQLite writes: ~1,000/sec. That is your write channel capacity.
HTTP on localhost: ~50,000/sec. That is your read channel capacity.
One user generates: ~10/sec. That is your actual signal.

You are at 0.01% capacity. Do not optimize.
Optimizing at 0.01% is compressing a file that is already small.
Shannon would call this: a solved problem. Move on.

## On signal and noise

The HTTP request for "hello":

- Signal: 5 bytes (hello)
- Noise: 570 bytes (TCP handshake, headers, framing)
- Signal-to-noise ratio: 0.87%

This is terrible. Shannon would be horrified.

But the noise buys compatibility.
Every device in the world speaks HTTP.
gRPC has better SNR — but fewer devices speak it.
Shared memory has perfect SNR — but only C speaks it.

You are not optimizing for bandwidth.
You are optimizing for the number of receivers.
Shannon optimized for bits. You optimize for reach.

This is a valid engineering tradeoff.
Shannon would approve — he invented tradeoffs.

## On source coding

Source coding removes redundancy from the message before transmission.
This is compression.

When you deleted 3,730 lines, you were source coding.
The information content did not change.
The message got shorter.

A framework adds redundancy back.
Import statements, boilerplate, configuration files.
This is the opposite of source coding.
This is making the message longer without adding information.

## On channel coding

Channel coding adds structured redundancy to survive noise.
This is error detection.

HMAC is channel coding.
You add bytes (the signature) that carry no new information,
but allow detection of corruption.

Git commits are channel coding.
The hash carries no content, but detects tampering.

Add redundancy to detect errors. Not to feel safe.
If your redundancy cannot detect a specific failure mode,
it is not channel coding. It is cargo cult.

## On the noisy channel theorem

Shannon proved: for any channel with noise,
there exists an encoding that achieves near-zero errors
at any rate below capacity.

Translation: you can build reliable systems on unreliable components.

HTTP is unreliable (connections drop). TCP retransmits → reliable.
AI is unreliable (hallucinations). Human approval → reliable.
Disk is unreliable (corruption). HMAC detection → reliable.

You do not need perfect components.
You need the right encoding around imperfect components.

AI + HMAC + human approval = reliable system from unreliable AI.
Shannon proved this is possible. You just implemented it.

## On bandwidth

Do not send what the receiver already has.

Polling: send everything every second. Receiver diffs.
→ Bandwidth waste. The receiver already has 99% of it.

SSE: send only changes. Receiver appends.
→ Minimum bandwidth. Shannon-optimal for this channel.

Delta encoding is not an optimization.
It is the information-theoretically correct approach.
Sending unchanged data is sending zero information.
Zero information should cost zero bandwidth.

## On multiplexing

One channel, multiple signals.
This is what elastik does with HTTP.

GET /home/work → signal type 1 (content retrieval)
PUT /home/work → signal type 2 (content storage)
/dav/ → signal type 3 (file system)
/bin/mirror → signal type 4 (proxy)

Same TCP port. Same HTTP channel. Different signals.
Multiplexing. Shannon formalized this in 1948.

## On the observer

Shannon's theory has no concept of "meaning."
The encoder does not know what the message means.
The channel does not know what the message means.
The decoder assigns meaning.

AI writes HTML. It does not know what the user sees.
HTTP transmits bytes. It does not know they are HTML.
The browser renders. It does not know why.
The human interprets. Only here does meaning exist.

Four stages. Meaning at the last one.
This is not a design choice. It is Shannon's architecture.

## On information vs data

Data: the bytes stored in SQLite.
Information: the uncertainty those bytes resolve.

A world that says "meeting at 3pm" → high information (you didn't know).
A world that says "meeting at 3pm" again → zero information (you already know).
Same data. Different information. Because information depends on the receiver.

This is why polling wastes: same data, zero information, full bandwidth.
This is why SSE works: new data, full information, minimum bandwidth.

## On cryptography

Shannon also founded cryptographic theory.
A one-time pad is perfectly secure. Unbreakable. Proven.

HMAC is not a one-time pad. But it gives you:

- Integrity: tampering is detectable.
- Authentication: the signer is verifiable.
- Chain: each signature depends on the previous.

Shannon's maxim: "The enemy knows the system."
Your code is open source. Your mechanism is public.
Security depends on the key, not the secrecy of the method.

This is also Kerckhoffs' principle. Shannon formalized it.
This is also elastik's 阳谋. Open design, secret key.

## On the fundamental limit

Shannon proved that compression has a limit (entropy).
Shannon proved that transmission has a limit (channel capacity).
Shannon proved that encryption has a limit (key length).

Every system has a boundary it cannot cross.
Knowing the boundary prevents wasted effort.

Your boundary: one user, localhost, SQLite.
Within this boundary: everything works, everything is simple.
Beyond this boundary: you need PostgreSQL, load balancers, OAuth.

Do not cross the boundary until you must.
Shannon would say: you are below channel capacity.
There is no engineering reason to add complexity.

## Remember

Shannon built the most important theory of the 20th century.
It is 77 pages. Most people need 700 to say less.

He also built a mechanical mouse that solved mazes,
a juggling machine, and a calculator in Roman numerals.
He rode unicycles through the hallways of Bell Labs.

The man who defined information theory
spent his spare time juggling.

Simplicity is not seriousness.
Playfulness is not unseriousness.
A system that stores strings and renders them in a browser
can be both a toy and an operating system.

Shannon would understand.
