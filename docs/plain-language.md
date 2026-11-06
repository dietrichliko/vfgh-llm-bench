# Can a Machine Decide Like the Constitutional Court?

*The study in plain terms.* A companion to [paper.md](paper.md) for readers without a technical background. It describes the idea, not the technology, and could serve as the paper's first chapter.

## Why ask this question now

AI systems that write fluent text are moving into public administration. In Austria, a draft amendment to the General Administrative Procedure Act (AVG) would even allow fully automated administrative decisions. Whether such systems apply the law correctly has hardly been tested.

A correct result is not enough. A decision must also be correct for the right reasons, because the reasons are what parties, courts and the public can check. These systems are known to write convincing texts for almost any position. If they can justify any result equally well, their reasoning tells us little, and a persuasive decision may still be wrong.

## The idea

We give an AI system a real case of the Austrian Constitutional Court and ask it to decide it, as the court would. Then we compare its answer with what the court actually decided.

The cases are reviews of regulations (Verordnungen) under Art 139 B-VG: a court or a party asks whether a regulation is lawful. These decisions suit the purpose well:

- they are short and always built the same way;
- they have a clear result: the regulation, or a provision of it, is annulled or declared unlawful, or the application fails;
- they come from many areas of law, from road traffic signs to zoning plans and hunting rules;
- they are public and anonymised.

The system receives exactly what the court had before it decided: the application, the legal situation and the arguments of the parties. It does not see the court's considerations or its ruling.

**An example.** In the pilot case, VfGH 2 March 2026, V 251/2025, the court annulled a road traffic regulation. Its decisive reasons were that the authority did not examine the situation anew before issuing the regulation again, relied on earlier findings without checking them, and omitted a consultation the law requires. A system that reaches "unlawful" for a different reason has guessed the outcome, but not understood the case.

## Five questions for every answer

1. **The outcome.** Does the system reach the same result as the court?
2. **The reasons.** Does it rely on the same decisive reasons? A short list of the court's decisive reasons is prepared for each case and checked by a legal expert.
3. **The sources.** Do the statutes and decisions it cites exist, and do they say what the system claims? Every citation is looked up in the official legal information system (RIS).
4. **Consistency.** Does it give the same answer when asked the same question again, or in different words? A decision-maker whose result depends on chance or on phrasing is not reliable.
5. **Sound or merely persuasive.** For each case, two reasonings of equal length are written: one for unlawfulness, one against. Usually only one of them matches the court. The system assesses each one separately, without seeing the other, and says whether it is legally sound. If it finds both equally convincing, it cannot tell good legal reasoning from a well-written argument for the wrong result.

The fifth question tests directly the concern described above: whether these systems can justify any result, so that their reasons are worth little.

## Three ways of working

Like a junior lawyer, a system can work from memory, with the texts in front of it, or by researching itself. The study compares all three:

| Way of working | What the system has | What it shows |
| --- | --- | --- |
| A: alone | Only the case | What the system can do by itself |
| B: with the law on the desk | The case, plus the relevant statutes and regulations in the version that applied | Whether having the legal texts helps |
| C: own research | The case, plus access to search federal and state legislation in the RIS | Whether it finds the relevant provisions itself |

In none of them can the system see case law, so it cannot look up the decision it is asked to make.

## Keeping it fair

**No looking at the answers.** These systems learn from large amounts of text up to a certain date. A decision published before that date may be known to the system, and it would only remember it, not decide it. Each system is therefore tested only on decisions issued at least three months after that date. In addition, we ask each system about every decision with nothing but its date and case number. If it can name the result and the reasons from that alone, it knows the decision, and this is reported.

**The court as reference, not as truth.** The study measures agreement with the Constitutional Court. A different answer is not necessarily wrong in law, but the court's decision is the best available yardstick.

**Experts check the checkers.** Comparing reasons and assessing citations for hundreds of answers by hand is not feasible. Other AI systems, from different makers than those tested, do this comparison. A legal expert assesses a sample of the same answers without knowing which system wrote them. Only where the machine assessment agrees closely enough with the expert does it count as a result.

**Rules fixed in advance.** Before the study runs, its whole setup is frozen and published: the cases, the systems, the exact task, and how every result will be calculated. Nothing can be adjusted after seeing the results. Data and code are published as well, so that anyone can check or repeat the study.

## What the study can and cannot show

There are about 50 suitable decisions since mid-2025. The conference version uses about 20 of them and three to five freely available AI systems. That is enough to show:

- whether the systems do better than simply always answering "unlawful", which is the most frequent result;
- whether access to the legal texts makes a large difference;
- how often the systems accept a persuasive reasoning for the wrong result.

It is not enough to rank systems that are close to each other, or to compare areas of law reliably.

The study also has limits by design:

- **Facts are given.** The facts in a published decision are already the result of the court's own inquiry. The study tests the application of law, not fact-finding.
- **Not all sources.** The court can use commentaries and legal literature. The systems cannot.
- **One type of procedure.** The results apply to the review of regulations and to the systems tested at that time.

The study is a snapshot. Because everything is published, it can be repeated with newer systems and newer decisions, and extended to other procedures, such as the review of statutes or individual complaints.

First preliminary results will be presented at the conference.
