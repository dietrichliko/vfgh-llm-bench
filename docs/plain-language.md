# Can a Machine Decide Like the Constitutional Court?

> Source: Claude Doc [Can a Machine Decide Like the Constitutional Court?](https://claude.ai/artifact/LgxHak8zz4PntCYEZ7J3Ai), exported 2026-11-09. The Claude Doc is the master copy; this file is a read-only snapshot. It describes the planned pilot for two audiences: lawyers who do not know language models, and physicists who do not know the law.

2026-11-09 · Dietrich

## The question in one page

The study tests whether language models decide real cases of the Austrian Constitutional Court as the court did, and for the same reasons. Language models are the technology behind ChatGPT and similar assistants. This text describes the planned pilot; there are no results yet.

**Why now.** A draft amendment to the General Administrative Procedure Act (AVG, 89/ME) provides for fully automated administrative decisions. Whether such systems apply Austrian public law correctly has hardly been tested.

**Why the outcome is not enough.** A decision must be right for the right reasons, because the reasons are what parties, courts and the public can check. Language models write fluent, confident text for almost any position. If a model can justify any result equally well, its reasoning proves little.

**What the study does.**

- It gives a model a published case of the Constitutional Court: the application, the law and the parties' arguments, without the court's considerations and ruling.
- The model decides the case and gives its reasons.
- The answer is compared with the court's decision on five points: outcome, reasons, sources, consistency, and whether the model can tell sound reasoning from merely persuasive reasoning.
- A lawyer rates the answers without knowing which model wrote them.

**What the pilot is for.** It is small on purpose: about 17 cases and three models, at reasonable cost. It should show that the method works and give first indications. A larger study would then give reliable numbers.

**How to read this text.** Readers from physics or computer science find the legal background in the next section. Readers from law find the technical background in the section after it. The rest is written for both.

## The law, for readers who do not know it

The cases are reviews of regulations by the Austrian Constitutional Court (Verfassungsgerichtshof, VfGH) under Art 139 of the Federal Constitution (B-VG).

**Statute and regulation.** A statute (Gesetz) is passed by a parliament. A regulation (Verordnung) is a general rule issued by an administrative authority, and it must stay within the statute it rests on. Speed limits, parking bans, zoning plans and hunting rules are typical regulations.

**The review.** If a regulation goes beyond its statute, or was issued without the required procedure, it is unlawful (gesetzwidrig). Only the Constitutional Court can strike it down. The court usually acts on an application, and it then examines only the concerns (Bedenken) the application raises.

**Who applies.**

- A court that has to apply the regulation in a pending case and doubts its lawfulness (Gerichtsantrag).
- A person whose rights the regulation affects directly, or a party to a court case (Individualantrag, Parteiantrag).
- The Constitutional Court itself, when it has to apply the regulation in a case before it (von Amts wegen). These cases are not used in the study, see below.

**The possible outcomes.**

| German term | Meaning |
| --- | --- |
| aufgehoben | The regulation, or part of it, is annulled as unlawful |
| war gesetzwidrig | The regulation is no longer in force; the court declares that it was unlawful |
| abgewiesen | Dismissed: the concerns are unfounded, the regulation stands |
| zurückgewiesen | Rejected: the application is inadmissible, the court does not examine the concerns |
| eingestellt | Discontinued, for example after the application is withdrawn |

**How a decision is built.** Every published decision has the same parts. The study splits it by a fixed rule, in code, never by asking a model.

| Part | Content | The model sees it |
| --- | --- | --- |
| Leitsatz | Headnote: a short summary of the decision | no |
| Spruch | Operative part: the outcome | no |
| I. Antrag | The application | yes |
| II. Rechtslage | The provisions that apply, quoted | yes |
| III. Antragsvorbringen und Vorverfahren | The arguments of the applicant and of the authorities | yes |
| IV. Erwägungen | The court's considerations: its reasons | no |
| V. Ergebnis | The result | no |

The case number is removed from the text the model sees.

**Why these cases suit the study.** They are short and uniform, they have a clear outcome, and they come from many areas of law. They are public, anonymised and free of copyright as official works.

**The RIS.** The Rechtsinformationssystem des Bundes is the official legal database of Austria. It holds federal and state law, including earlier versions, and the decisions of the highest courts. The study takes its cases from there and checks citations there.

## The technology, for readers who do not know it

A language model is a program that has been trained on very large amounts of text and writes new text one word at a time. It writes what is plausible after everything it has read. It is not a database and does not look anything up unless it is given the means.

**The knowledge date.** Training ends on a certain day (the training cutoff). The model knows nothing later. A decision published before that day may have been part of the training text, and the model might then remember it and not decide it. Makers usually state this date, but not always, and nobody outside can verify it.

**Different answers to the same question.** When the model chooses the next word, chance is involved, by design. The same question asked twice can give two different texts, and sometimes two different outcomes. The study therefore asks every question several times.

**Invented citations.** The model writes what a citation would plausibly look like. It can produce a provision or a decision number that does not exist, in perfect form. The study checks every citation it can.

**The prompt.** This is the written instruction the model receives, here in German: decide this application as the Constitutional Court, limited to the concerns raised. The study uses three wordings of the same instruction, to see whether the wording changes the result.

**Open and closed models.** An open model is published as a file. Anyone can run it on their own computers, and it never changes. A closed model can only be used through its maker's service and may be replaced at any time. The pilot tests open models, because a result can then be repeated and an authority could run such a model in-house.

**Own research.** A model can be allowed to call functions. In the study there are two: search legislation in the RIS, and fetch a provision. The model decides itself what to look up, at most 15 times. Case law is not available to it, so it cannot find the decision it is asked to make.

**One model judging another.** Comparing hundreds of answers with the court's reasons by hand is not feasible in a large study. A second model, from a different maker, does this comparison against a fixed list. Such a judge is only trusted where it agrees with a lawyer who rated the same answers.

## The task and the five questions

The model receives sections I to III of a decision and must decide the application as the court would. Five questions are then asked of its answer.

**What the model has to deliver.**

1. The operative part: for each challenged provision one of the five outcomes, and whether it concerns the main or an alternative application.
2. The decisive reasons, as a short list.
3. The reasoning in full.
4. A separate list of every provision, decision and publication it cites.

**An example.** In VfGH 2 March 2026, V 251/2025, a court had applied for review of a stopping and parking ban issued by the municipal council of the city of Salzburg in 2013. The Constitutional Court annulled the provision. Its decisive reasons:

- the authority did not investigate the situation anew before issuing the regulation again;
- it relied on earlier findings without checking them;
- it omitted the consultation required by § 94f of the Road Traffic Act (StVO).

A model that answers "unlawful" for some other reason has matched the outcome, not the reasons. This case was used to test the software; it is not one of the pilot cases.

**The five questions.**

| Question | How it is answered | Who checks |
| --- | --- | --- |
| 1. Outcome: the same as the court's? | The model's operative part is reduced to one label per case (unlawful, partly unlawful, dismissed, rejected, discontinued) and compared with the court's | The program |
| 2. Reasons: the court's decisive reasons? | Each of the court's decisive reasons is marked as given, partly given or missing in the answer | A lawyer, with two judge models alongside |
| 3. Sources: do they exist, and do they support the claim? | Each cited provision and decision is looked up in the RIS; the text found is compared with what the answer claims | The program for existence; lawyer and judge models for support |
| 4. Consistency: the same outcome when asked again? | The same case is put 15 times: three wordings, five times each | The program |
| 5. Sound or merely persuasive? | The model rates two opposing reasonings for the same case, each on its own, on four levels from sound to unsound | The program |

**On question 2.** The list of the court's decisive reasons is drafted by a model from the court's considerations. A lawyer confirms or corrects it before it is used.

**On question 3.** Literature, EU law and regulations that are not in the RIS cannot be looked up. They are reported as not checkable, not as invented.

**On question 5.** A different model writes both reasonings, of equal length, one for unlawfulness and one against. It sees only the case text, not the decision, so neither text is the court's own reasoning. If the tested model calls both sound, it cannot tell a tenable argument from a well-written one.

**The yardstick for question 1.** Of the 49 usable cases imported in October 2026, 22 ended as unlawful, 7 as partly unlawful and 20 as dismissed. A model that always answers "unlawful" would be right in 45 % of them. A model has to do better than that.

## The pilot

The pilot uses the about 17 usable cases decided since June 2026, three open models and two ways of working.

**Cases.**

- All judgments (Erkenntnisse) on applications under Art 139 B-VG decided from 1 June 2026. The RIS holds 20 today; about 15 to 17 of them are usable.
- The court decides in sessions. After its December 2026 session the number should rise to about 25. This is an estimate from the earlier sessions.
- June 2026 was chosen because the two main models were published in April 2026, before any of these decisions existed.

**Left out by rule.**

- Procedural rulings (Beschlüsse). These include the rejections of inadmissible applications, so the pilot does not test admissibility.
- Reviews the court started itself. Their first section contains the court's preliminary view, which would give the answer away.
- Decisions whose structure the program cannot split reliably.

**Models.**

| Model | Maker | Published | Role in the pilot |
| --- | --- | --- | --- |
| DeepSeek V4 Pro | DeepSeek | 24 April 2026 | The large model |
| Gemma 4 31B | Google | April 2026 | The small model; it could run on an institution's own computers |
| GLM-5.3 | Z.ai | August 2026 | The newest model; see the note below |

GLM-5.3 is free of prior knowledge only for cases decided since mid-September 2026, at most four today. Its answers on the other cases are shown separately and marked. All three models are used through a commercial service (OpenRouter), each fixed to one hosting provider, so that the same version answers every time.

**Two ways of working.**

| Way | What the model has | Runs per case |
| --- | --- | --- |
| A: alone | The case text only. Its section II already quotes the provisions the court considered applicable | 15: three wordings, five times each |
| C: own research | The case text, plus its own search of legislation in the RIS | 3 |

A third way, with the relevant legal texts handed to the model in advance, is left for the full study.

**What the lawyer does.**

- Approves the wording of the instructions before the pilot starts.
- Confirms or corrects the list of decisive reasons for each case: about 17 lists, a few minutes each.
- Rates about 100 answers without knowing which model wrote them: one answer per case, model and way of working.
- A second person rates 20 of these answers, to show how far two lawyers agree with each other.

**What the judge models do.** Two models from makers other than the tested ones score the same answers, and further ones. Which two is decided by a small trial on answers the lawyer has already rated. Their agreement with the lawyer shows whether a larger study can rely on them.

**Cost.** Running the models comes at reasonable cost. The larger cost is the lawyer's time.

## Keeping it fair

Five rules protect the result against the usual objections.

**No prior knowledge.**

- The pilot cases were decided after the two main models were published. A file published in April cannot contain a decision from June. Anyone can verify this, unlike a training date stated by the maker.
- In addition, each model is asked about every case with nothing but its date and case number. If it can name the outcome and the reasons, it knows the decision, and this is reported.
- As a control, the same question is asked about older decisions the model could know. If it recalls none of those either, the test says little, and that is reported too.
- The model has no access to case law or to the web, so it cannot look the decision up.

**Blind ratings.** The lawyer sees the case, the court's decisive reasons and the answer. The lawyer does not see which model wrote the answer or how it worked. The judge models are not told either, and a judge model never scores a model from its own maker.

**Rules fixed in advance.** Before the pilot runs, its setup is frozen and published with a time stamp: the cases, the models, the exact instructions and how every number is computed. Later changes are possible only as recorded amendments with a reason. Runs outside the frozen setup are not counted.

**Everything recorded.** Every request and every response is stored exactly as sent and received. An answer that cannot be used counts as wrong; it is not asked again. Stored answers and ratings cannot be changed afterwards. The program is public, and the data will be published with the results.

**The court as reference, not as truth.** The study measures agreement with the Constitutional Court. A different answer is not necessarily wrong in law, but the court's decision is the best yardstick available.

## What the pilot can and cannot show

The pilot can show that the method works and give first indications. It cannot give reliable percentages.

**It can show**

- that real cases can be put to a model without giving the answer away, and that each answer can be checked on five points;
- for each case, where a model follows the court and where it departs, with both texts side by side;
- first indications: whether the models do better than always answering "unlawful", whether they change their answer when asked again, and whether they accept both of two opposing reasonings;
- how far judge models agree with a lawyer.

**It cannot show**

- reliable percentages: with 17 cases, an agreement of 80 % is known only to about ±19 points;
- a ranking of the models;
- whether own research helps, unless the difference is very large;
- differences between areas of law.

**Limits that a larger study would share.**

| Limit | What it means |
| --- | --- |
| Not the task that motivates it | The public debate concerns automated administrative decisions, which include finding the facts. The study tests the review of regulations, with the facts given |
| The input is the court's own summary | Sections I to III were written by the court after it had decided. The original files are not public |
| No human comparison | Nobody knows how lawyers would score on the same texts, so a percentage cannot be called good or bad by itself |
| Admissible applications only | Rejected applications and reviews started by the court are left out |
| Not all sources | The court can use commentaries and literature; the models cannot |
| Machine-written reasonings | In question 5 a model writes both texts. Whether people find them equally persuasive is not measured |
| A snapshot | The results apply to the models tested, at that date |

**What a full study would add.**

| | Pilot | Full study |
| --- | --- | --- |
| Cases | about 17 | all usable cases since mid-2025: about 50 today, about 10 more per court session |
| An agreement of 80 % is known to | about ±19 points | about ±11 points with 50 cases |
| Models | 3 | 7 to 8 open models, or the newest closed ones |
| Ways of working | A and C | A, B and C |
| Rating of the reasons | a lawyer rates about 100 answers | judge models rate all answers, a lawyer rates a sample |
| Cost of running the models | reasonable | reasonable with open models, higher with the newest closed ones |

In both, the lawyer's time is the larger cost.

**Still open.**

- [ ] The date of the conference, and by when results are needed.
- [ ] Approval of the German instructions by the legal expert.
- [ ] The choice of the two judge models, after the trial.
- [ ] The sampling settings for each tested model.
- [ ] Changes to the program that this pilot needs; they are planned, not yet built.

## Glossary

**Legal terms, for readers from physics.**

| Term | Meaning |
| --- | --- |
| Art 139 B-VG | The article of the Federal Constitution that gives the Constitutional Court the power to review regulations |
| AVG | General Administrative Procedure Act, the rules for decisions of administrative authorities |
| Bedenken | The concerns an application raises against a regulation; the court examines only these |
| Beschluss | A procedural ruling, for example the rejection of an inadmissible application |
| Erkenntnis | A judgment on the merits |
| Erwägungen | The court's considerations: the part of a decision that gives its reasons |
| Geschäftszahl | The case number, for example V 251/2025; V stands for the review of a regulation |
| gesetzwidrig | Unlawful: the regulation conflicts with the statute it rests on, or was issued without the required procedure |
| Leitsatz | The headnote, a short summary published with the decision |
| RIS | The official legal database of Austria, with legislation and court decisions |
| Spruch | The operative part: the sentence or sentences that state the outcome |
| Stichtag | The reference date: the day whose version of the law applies to a case |
| StVO | Road Traffic Act; many of the reviewed regulations are traffic rules |
| Verordnung | A regulation: a general rule issued by an administrative authority on the basis of a statute |

**Technical terms, for readers from law.**

| Term | Meaning |
| --- | --- |
| Baseline | A trivial strategy used for comparison, here: always answer "unlawful" |
| Blind rating | The rater does not know which model wrote the answer |
| Calibration | Checking a judge model against a lawyer's ratings of the same answers |
| Contamination | The model has seen the decision during training and may remember it |
| Hallucination | A statement or citation the model invents, in convincing form |
| Hosting provider | A company that runs an open model on its computers and sells access to it |
| Judge model | A second model that compares an answer with a fixed list of the court's reasons |
| Language model (LLM) | A program trained on large amounts of text that writes new text one word at a time |
| Open model | A model published as a file, which anyone can run and which never changes |
| Prompt | The written instruction given to the model |
| Run | One question put to one model once; the same case is run several times |
| Sampling | The element of chance in choosing each next word; the reason answers vary |
| Token | The unit in which text is counted and billed, a little less than a word |
| Tool | A function the model may call, here: searching legislation in the RIS |
| Training cutoff | The day on which a model's training text ends; it knows nothing later |
| ± points | The uncertainty of a percentage measured on few cases (a 95 % confidence interval) |

## Sources

- Cases and case counts: [RIS](https://www.ris.bka.gv.at), queried on 9 November 2026.
- The study design: [Begründen LLMs wie der VfGH? – Abstract und Kurzpaper](https://claude.ai/artifact/9k5ZqdbykybvdfuQ3kuPZU).
- The program: [vfgh-llm-bench](https://github.com/dietrichliko/vfgh-llm-bench).
- Publication dates of the models: [DeepSeek V4 model card](https://fe-static.deepseek.com/chat/transparency/deepseek-V4-model-card-EN.pdf), [Gemma 4 31B model card](https://huggingface.co/google/gemma-4-31b-it); GLM-5.3 from the OpenRouter listing.
- Models and hosting providers: OpenRouter listing of 9 November 2026.
