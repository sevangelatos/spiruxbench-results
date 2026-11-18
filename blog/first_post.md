## Intro


The last year or so I have been fascinated by local AI. There is something magical and unreal in watching your own computer work on a problem on its own or using it as an interactive helper. Cloud AI I feel doesn't have the same feel. It feels like you are handing the wheel to someone else. In any case, I was looking for a way to measure the capabilities of models that are realistic to run on a gaming PC. I wanted also to understand which, if any of the fine tunes of Qwen 3.6 35b could be used as a replacement of the 3.8 MoE that we never got. Things escalated from there.

## How did you build this?

It is a mostly vibe coded harness to benchmark models. It is built around pi agent. The agent works in pi, with 100k context and under a timeout constraint. It is working in a network isolated container that only communicates with the host over a named pipe. The challenges are graded in another container based on hidden automated tests. A full run takes about 10 hours.

My initial attempt was just assigning MiMO v2.6 Flash with coming up with 100 different python and C++ projects to work on. The projects looked pretty challenging. ...meh, turns out not so much. I guess all of the models are trained on the same basic challenges. Even very weak models could ace through them. I got a new degree of respect for the small models but a benchmark that most models ace is useless. 

I took gemma4 E4B (an admittedly weak model for agentic programming). Had it try to solve all of them and just threw away all of the challenges it passed. A side effect of that is that my benchmark might be a bit biased against the gemma4 family. After that, I gradually built up again the challenges based on brainstorming mini project ideas and on adapting with the help of Sol, recent CVEs that we would turn into challenges. I had to aggressively review the tasks for blatant giveaways that the models would leave behind in the prompts or in the public tests. Overall I am happy with the result. I think it correlates well with my own experience working with these models for coding tasks.

## Did you find the Qwen 3.6 35B successor?

Maybe. 
